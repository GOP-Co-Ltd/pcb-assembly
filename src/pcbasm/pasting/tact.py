"""塗布タクトタイムの事前見積り.

装置に触れずに、基板設定から決まる塗布対象（pad と解決済み設定）だけで塗布ループの
所要時間を積算する。ジョブを開始する前に「どれくらいかかるか」を出すのが目的なので、
Klipper もステージも参照しない。

1 成分ぶんの内訳は ``FillSequence.to_gcode`` が組む動作と同じ順序:

1. 直前の終点から始点上空への XY 移動
2. 塗布高さまでの下降（純 Z 移動）
3. prime + 吐出（:attr:`~pcbasm.pasting.fill_sequence.FillSequence.dispense_duration`）
4. リトラクションと Z 上昇（同時に始まるので長い方）

ステージ移動と最後のリトラクションは、``wait_for_done`` / ``G4 P0`` / ``sync`` を挟んで
停止して終わるので、加速 → 巡航 → 減速で見積もる（:func:`_move_duration`）。2 の下降だけは
1 の XY 移動と続けて queue されるため lookahead が繋ぐ余地があり、そのぶん多く見る
（XY → 純 Z は直角の向き変更なので、繋がっても僅か）。吐出そのものは次の動作へ連続するので、
``FillSequence`` 側の ramp モデル（減速を含まない）をそのまま使う。

XY と Z で速度・加速度を分けるのは、kinematics が Z 成分を含む移動を Z 軸の上限へ
落とすため（``klipper/inversed_corexy.py``）。下降と上昇は純 Z 移動で、XY の上限で
見積もると大きく短く出る。

board 座標のまま計算する。board → machine 変換は回転と平行移動だけなので経路長が
変わらない。線の走行方向（``line_reference``）は向きしか変えないので渡さない。
"""

from __future__ import annotations

import math
from collections.abc import Iterable

import attrs

from pcbasm.config import PasteDispenser, Tact
from pcbasm.geometry import Path, Point2d
from pcbasm.pasting.fill_path import FillPlan
from pcbasm.pasting.fill_sequence import FillSequence
from pcbasm.pasting.params import DispenseSettings, PasteParams
from pcbasm.pcb import Pad


def _move_duration(distance: float, speed: float, accel: float) -> float:
    """停止 → 加速 → 巡航 → 減速 → 停止で ``distance`` を走る時間 [sec].

    巡航速度に達しない短い移動は三角プロファイル（加速して即減速）になる。
    """
    if distance <= 0.0:
        return 0.0
    ramp = speed**2 / accel  # 加速と減速で使い切る距離
    if distance <= ramp:
        return 2.0 * math.sqrt(distance / accel)
    return 2.0 * speed / accel + (distance - ramp) / speed


@attrs.frozen
class TactEstimate:
    """塗布ジョブ 1 回の所要時間見積り.

    Attributes:
        setup_sec: 位置合わせ・高さ計測など pad 数に依らない固定分 [sec]
        dispense_sec: 塗布ループ（移動 + 吐出）の合計 [sec]
        pad_count: 見積り対象の pad 枚数
    """

    setup_sec: float
    dispense_sec: float
    pad_count: int

    @property
    def total_sec(self) -> float:
        """見積りの総所要時間 [sec]."""
        return self.setup_sec + self.dispense_sec


def estimate_paste_tact(
    targets: Iterable[tuple[Pad, PasteParams]],
    *,
    config: PasteDispenser,
    tact: Tact,
    start: Point2d = Point2d(0.0, 0.0),
) -> TactEstimate:
    """塗布対象から塗布ジョブの所要時間を見積もる.

    Args:
        targets: 塗布順に並んだ (pad, 解決済み塗布パラメータ) の列
        config: ``machine.toml`` の ``[paste_dispenser]``
        tact: 見積り用の移動速度と固定オーバーヘッド
        start: 塗布ループ開始時のノズル位置（board 座標）
    """
    settings = DispenseSettings.from_config(config)
    retract_sec = _move_duration(
        settings.retract_amount, settings.retract_rate, settings.retract_accel
    )
    lift_sec = _move_duration(settings.lift_height, tact.z_speed, tact.z_accel)
    current = start
    dispense_sec = 0.0
    pad_count = 0
    for pad, params in targets:
        pad_count += 1
        plan = FillPlan.for_pad(pad.polygon, config=config, params=params)
        amount_ul = plan.component_amount_ul(pad.polygon, params)
        for points in plan.paths:
            sequence = FillSequence(
                path=Path(p.to3d(params.paste_height_mm) for p in points),
                total_amount_ul=amount_ul,
                settings=settings,
                prime_extra_delay=params.prime_extra_delay,
            )
            dispense_sec += (
                _move_duration(
                    (points[0] - current).norm, tact.travel_speed, tact.travel_accel
                )
                + lift_sec  # 塗布高さまでの下降
                + sequence.dispense_duration
                + max(retract_sec, lift_sec)  # リトラクションと上昇は同時に始まる
            )
            current = points[-1]
    return TactEstimate(
        setup_sec=tact.setup_sec, dispense_sec=dispense_sec, pad_count=pad_count
    )
