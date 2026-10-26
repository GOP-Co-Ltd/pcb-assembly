"""塗布の所要時間の予測（タクトタイム）.

装置を動かさずに、塗布計画と printer.cfg の速度・加速度の上限から、塗布ジョブにかかる時間を見積もる。

経路 1 本の時間は :mod:`pcbasm.pasting.fill_sequence` が組む動作の順に積む。

1. 直前の経路の終点から、次の経路の始点の上空への XY 移動
2. 塗布高さへの下降
3. prime と吐出（:attr:`~pcbasm.pasting.fill_sequence.FillSequence.dispense_seconds`）、塗布後の静定待ち
4. retract と上昇（同時に始まるので長いほう）

微小線は :class:`~pcbasm.pasting.fill_sequence.MicroLineSequence` の順（上空で押し出し → 静定待ち → 下降 → こすりつけ → 静定待ち → 上昇 → retract）に積む。

経路はどれも M400 で止まって終わるので、ステージとディスペンサーの移動は止まった状態から止まるまでの台形速度プロファイルで見積もる。
XY と Z は上限を分ける。kinematics（``klipper/inversed_corexy.py``）が Z を含む移動を Z の上限へ落とすので、下降と上昇を XY の上限で見積もると大きく短く出る。

位置合わせ・高さ計測・初回パージ・運転時補正、最初の経路への移動、終了後の退避など、pad 数に依らない前段は ``[solder.tact] setup_sec`` で足す。
board 座標のまま計算する（board → machine の変換は回転と平行移動なので、経路長が変わらない）。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Self

import attrs

from pcbasm.config.solder import TactConfig
from pcbasm.geometry import Path, Point3d
from pcbasm.pasting.fill_path import amount_per_path_ul
from pcbasm.pasting.fill_sequence import FillSequence
from pcbasm.pasting.params import DispenseMotion
from pcbasm.pasting.plan import PastePlan


@attrs.frozen
class MotionLimits:
    """ステージの速度と加速度の上限（printer.cfg の ``[printer]``）.

    Attributes:
        velocity: XY の速度 [mm/sec]（``max_velocity``）
        accel: XY の加速度 [mm/sec²]（``max_accel``）
        z_velocity: Z の速度 [mm/sec]（``max_z_velocity``）
        z_accel: Z の加速度 [mm/sec²]（``max_z_accel``）
    """

    velocity: float
    accel: float
    z_velocity: float
    z_accel: float

    @classmethod
    def from_printer_config(cls, config: Mapping[str, Mapping[str, Any]]) -> Self:
        """printer.cfg の内容から読む（Z を省いた printer.cfg は Klipper と同じく XY の値を使う）."""
        printer = config["printer"]
        velocity = float(printer["max_velocity"])
        accel = float(printer["max_accel"])
        return cls(
            velocity=velocity,
            accel=accel,
            z_velocity=float(printer.get("max_z_velocity", velocity)),
            z_accel=float(printer.get("max_z_accel", accel)),
        )


@attrs.frozen
class TactEstimate:
    """塗布ジョブ 1 回の所要時間の予測.

    Attributes:
        setup_seconds: pad 数に依らない前段 [sec]
        paste_seconds: pad を塗る時間の合計 [sec]
        pad_count: 塗る pad の数
    """

    setup_seconds: float
    paste_seconds: float
    pad_count: int

    @property
    def total_seconds(self) -> float:
        """所要時間 [sec]."""
        return self.setup_seconds + self.paste_seconds


def estimate_paste_tact(
    plan: PastePlan, tact: TactConfig, limits: MotionLimits
) -> TactEstimate:
    """塗布計画の有効 pad を塗る順に積み、塗布ジョブの所要時間を予測する."""
    motion = plan.config.dispense_motion
    seconds = 0.0
    previous: Point3d | None = None
    for pad in plan.routed_pads:
        fill = plan.fill_plan(pad)
        params = plan.params_for(pad)
        amount_ul = amount_per_path_ul(pad.polygon, params, len(fill.paths))
        for points in fill.paths:
            path = Path(points)
            if previous is not None:
                distance = (path[0].to2d() - previous.to2d()).norm
                seconds += _move_seconds(distance, limits.velocity, limits.accel)
            previous = path[-1]
            if fill.dispense_mode == "micro_line":
                seconds += _micro_line_seconds(path, amount_ul, motion, limits)
            else:
                sequence = FillSequence(
                    path=path,
                    total_amount_ul=amount_ul,
                    motion=motion,
                    prime_extra_delay=params.prime_extra_delay,
                )
                seconds += _fill_seconds(sequence, motion, limits)
    return TactEstimate(
        setup_seconds=tact.setup_sec, paste_seconds=seconds, pad_count=len(plan.route)
    )


def _fill_seconds(
    sequence: FillSequence, motion: DispenseMotion, limits: MotionLimits
) -> float:
    """下降 → prime と吐出 → 静定待ち → retract と上昇（同時に始まるので長いほう）."""
    lift = _lift_seconds(motion, limits)
    return (
        lift
        + sequence.dispense_seconds
        + motion.post_dispense_settle_seconds
        + max(_retract_seconds(motion), lift)
    )


def _micro_line_seconds(
    path: Path, amount_ul: float, motion: DispenseMotion, limits: MotionLimits
) -> float:
    """上空で押し出し → 静定待ち → 下降 → こすりつけ → 静定待ち → 上昇 → retract."""
    push = _move_seconds(
        motion.retract_amount + amount_ul,
        motion.max_dispense_rate,
        motion.dispense_accel,
    )
    scrub = _move_seconds(path.length(), motion.max_fill_speed, limits.accel)
    lift = _lift_seconds(motion, limits)
    return (
        push
        + motion.micro_line_settle_seconds
        + lift
        + scrub
        + motion.post_dispense_settle_seconds
        + lift
        + _retract_seconds(motion)
    )


def _lift_seconds(motion: DispenseMotion, limits: MotionLimits) -> float:
    """塗布高さと上空のあいだの Z 移動 1 回."""
    return _move_seconds(motion.lift_height, limits.z_velocity, limits.z_accel)


def _retract_seconds(motion: DispenseMotion) -> float:
    return _move_seconds(
        motion.retract_amount, motion.retract_rate, motion.retract_accel
    )


def _move_seconds(distance: float, velocity: float, accel: float) -> float:
    """止まった状態から ``distance`` を進んで止まるまでの時間 [sec]（台形速度プロファイル）.

    巡航速度に届かない短い移動は、加速してすぐ減速する三角形になる。
    """
    if distance <= velocity**2 / accel:
        return 2.0 * math.sqrt(distance / accel)
    return velocity / accel + distance / velocity
