"""塗布の所要時間の予測（タクトタイム）.

装置を動かさずに、塗布計画と printer.cfg の速度・加速度の上限から、塗布ジョブにかかる時間を見積もる。
時間は、pad を塗り始めるまでの前段（:func:`estimate_prelude`）と、pad を塗る時間（:func:`estimate_paste_tact`）に分ける。

前段は塗布ジョブの手順の順に積む。

1. 基準点の計測: 左上 → 右上 → 右下 → 左下の基準点を回り、各所で円検出（``[detection] sample_count`` 枚）と位置補正を繰り返す
2. 高さ計測: 銅箔の上に計画した点（:func:`~pcbasm.pasting.height.plan_probe_points`）を近い順に回り、各点で PROBE する
3. 位置合わせ: pad を覆う照合領域（:func:`~pcbasm.align.plan_alignment_regions`）と、短辺の小さい pad の中心（:func:`~pcbasm.align.refinement_targets`）を回り、各所で撮影と照合を繰り返す
4. 初回パージと流量キャリブレーション: カメラで撮る点とノズルで塗る点（ツールヘッドオフセットだけずれる）を回り、撮影・点塗布・静定待ちをする

移動の距離・速度・加速度、静定待ち、撮影のフレーム数は設定と計画から求める。
1 か所を合わせるまでの撮影回数、撮影 1 回の処理、PROBE 1 回の時間、基板に依らない固定分は ``[solder.tact]`` で持つ（:class:`~pcbasm.config.solder.TactConfig`）。
カメラの起動・ホーミング・カメラ回転角の計測、前段の各段のあいだの移動（カメラとノズルの持ち替え・Z の上げ下げ）は固定分に含める。
ノズルクリーニングは初回パージが写らないときだけ行うので含めない。
カメラが撮影位置へ届かない・校正ファイルが読めないなどで実機が撮影を飛ばす場合も、撮影するものとして数える。

pad を塗る時間は、経路 1 本ずつ :mod:`pcbasm.pasting.fill_sequence` が組む動作の順に積む。

1. 直前の経路の終点から、次の経路の始点の上空への XY 移動
2. 塗布高さへの下降
3. prime と吐出（:attr:`~pcbasm.pasting.fill_sequence.FillSequence.dispense_seconds`）、塗布後の静定待ち
4. retract と上昇（同時に始まるので長いほう）

微小線は :class:`~pcbasm.pasting.fill_sequence.MicroLineSequence` の順（上空で押し出し → 静定待ち → 下降 → こすりつけ → 静定待ち → 上昇 → retract）に積む。

移動はどれも止まって終わるので、ステージとディスペンサーの移動は止まった状態から止まるまでの台形速度プロファイルで見積もる。
XY と Z は上限を分ける。kinematics（``klipper/inversed_corexy.py``）が Z を含む移動を Z の上限へ落とすので、下降と上昇を XY の上限で見積もると大きく短く出る。

board 座標のまま計算する（board → machine の変換は回転と平行移動なので、経路長が変わらない）。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, Self

import attrs

from pcbasm.align import (
    BoardFrame,
    CopperProjector,
    plan_alignment_regions,
    refinement_targets,
)
from pcbasm.align.board_frame import Corner, marker_position
from pcbasm.board import Board, Layer
from pcbasm.config import MachineConfig
from pcbasm.geometry import Identity, Path, Point2d, Point3d, sort_by_nearest
from pcbasm.pasting.dot_capture import PRIME_EXTRA_DELAY
from pcbasm.pasting.fill_path import amount_per_path_ul
from pcbasm.pasting.fill_sequence import FillSequence
from pcbasm.pasting.height import plan_probe_points
from pcbasm.pasting.params import DispenseMotion
from pcbasm.pasting.plan import PastePlan
from pcbasm.vision import CameraModel

# 基準点の計測と高さ計測の移動は max_velocity の 0.9 倍（BoardTransformMeasurer と HeightPlaneMeasurer の既定）
_MEASURE_VELOCITY_RATIO = 0.9
# 銅箔照合の移動は max_velocity の半分（RegionAligner）
_ALIGN_VELOCITY_RATIO = 0.5
# 基準点を回る順（BoardTransformMeasurer）
_CORNERS = (Corner.TOP_LEFT, Corner.TOP_RIGHT, Corner.BOTTOM_RIGHT, Corner.BOTTOM_LEFT)


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
class PreludeEstimate:
    """塗布ジョブの開始から pad を塗り始めるまで（前段）の予測.

    固定分には、塗り終えた後のノズルキャップへの退避も含める（pad 数に依らないため）。

    Attributes:
        fixed_seconds: 基板に依らない固定分 [sec]（``[solder.tact] fixed_sec``）
        reference_seconds: 基準点 4 か所の計測 [sec]
        probe_count: 高さ計測の点数
        probe_seconds: 高さ計測 [sec]
        align_count: 位置合わせで合わせる箇所の数（照合領域と、中心で照合する pad）
        align_seconds: 位置合わせ [sec]
        purge_seconds: 初回パージと流量キャリブレーション [sec]
    """

    fixed_seconds: float
    reference_seconds: float
    probe_count: int
    probe_seconds: float
    align_count: int
    align_seconds: float
    purge_seconds: float

    @property
    def total_seconds(self) -> float:
        """前段の合計 [sec]."""
        return (
            self.fixed_seconds
            + self.reference_seconds
            + self.probe_seconds
            + self.align_seconds
            + self.purge_seconds
        )


@attrs.frozen
class TactEstimate:
    """塗布ジョブ 1 回の所要時間の予測.

    Attributes:
        prelude: pad を塗り始めるまでの前段
        paste_seconds: pad を塗る時間の合計 [sec]
        pad_count: 塗る pad の数
    """

    prelude: PreludeEstimate
    paste_seconds: float
    pad_count: int

    @property
    def total_seconds(self) -> float:
        """所要時間 [sec]."""
        return self.prelude.total_seconds + self.paste_seconds


def estimate_prelude(
    plan: PastePlan,
    board: Board,
    machine: MachineConfig,
    limits: MotionLimits,
    camera_model: CameraModel,
) -> tuple[PreludeEstimate | None, str | None]:
    """塗布ジョブが pad を塗り始めるまでの前段を予測する.

    高さ計測の点を計画できない基板（銅箔が無い・小さい）は、塗布ジョブも高さ計測で止まるので、予測せずに理由を返す。

    位置合わせの前なので、board 座標をそのまま機械座標とみなす（照合領域の数と並びは平行移動に依らない）。
    ``camera_model`` は照合領域の大きさ（画素の大きさ）を決めるのに使う。
    """
    tact = machine.require_solder().tact
    settle = machine.settle
    fps = machine.camera.fps
    # 撮影 1 回: 移動後の静定待ち + フレームを撮る時間 + 処理
    circle = settle.move_sec + machine.detection.sample_count / fps + tact.capture_sec
    frame = settle.move_sec + 1.0 / fps + tact.capture_sec
    measure_velocity = limits.velocity * _MEASURE_VELOCITY_RATIO

    markers = [
        marker_position(board.outline, machine.reference_point.offsets, corner)
        for corner in _CORNERS
    ]
    reference = (
        _tour_seconds(markers, measure_velocity, limits.accel)
        + len(markers) * tact.align_passes * circle
    )

    try:
        planned = plan_probe_points(
            (copper for copper in board.copper if copper.layer == Layer.TOP),
            board.outline.polygon,
            config=machine.probe,
        )
    except ValueError as exc:
        return None, f"高さ計測の点を計画できません: {exc}"
    probes = sort_by_nearest(
        planned, markers[-1].to3d(), key=lambda point: point.to3d()
    )
    probe = _tour_seconds(probes, measure_velocity, limits.accel) + len(probes) * (
        settle.move_sec + settle.probe_sec + tact.probe_point_sec
    )

    targets = _align_targets(
        plan, board, machine, camera_model, start=probes[-1] if probes else markers[-1]
    )
    align = (
        _tour_seconds(targets, limits.velocity * _ALIGN_VELOCITY_RATIO, limits.accel)
        + len(targets) * tact.align_passes * frame
    )

    prelude = PreludeEstimate(
        fixed_seconds=tact.fixed_sec,
        reference_seconds=reference,
        probe_count=len(probes),
        probe_seconds=probe,
        align_count=len(targets),
        align_seconds=align,
        purge_seconds=_purge_seconds(plan, limits, frame),
    )
    return prelude, None


def estimate_paste_tact(
    plan: PastePlan, prelude: PreludeEstimate, limits: MotionLimits
) -> TactEstimate:
    """塗布計画の有効 pad を塗る順に積み、前段を足して塗布ジョブの所要時間を予測する."""
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
        prelude=prelude, paste_seconds=seconds, pad_count=len(plan.route)
    )


def _align_targets(
    plan: PastePlan,
    board: Board,
    machine: MachineConfig,
    camera_model: CameraModel,
    *,
    start: Point2d,
) -> list[Point2d]:
    """位置合わせで撮る箇所を撮る順に並べる（照合領域の中心 → 中心で照合する pad の中心）."""
    alignment = machine.alignment
    pads = plan.routed_pads
    regions = plan_alignment_regions(
        CopperProjector.from_board_frame(BoardFrame(board, camera_model, Identity())),
        Identity(),
        [pad.center for pad in pads],
        safe_area=board.outline.polygon.buffer(-alignment.board_edge_margin),
        roi=camera_model.centered_roi(alignment.region_size_px),
        overlap=alignment.region_overlap,
        tour_start=start,
    )
    refined = refinement_targets(
        pads, max_short_side_mm=alignment.refine_max_short_side
    )
    return [region.board_center for region in regions] + [pad.center for pad in refined]


def _purge_seconds(plan: PastePlan, limits: MotionLimits, capture: float) -> float:
    """初回パージ（撮影 → 点塗布 → 撮影）と流量キャリブレーション（全点撮影 → 全点塗布 → 静定待ち → 全点撮影）."""
    offset = plan.config.nozzle.offset
    nozzle = Point2d(offset.x, offset.y)
    motion = plan.config.dispense_motion
    # 回る順に (ステージの位置, そこでかかる時間)。塗る点はカメラで撮る点からツールヘッドオフセットだけずれる
    stops: list[tuple[Point2d, float]] = []
    wait = 0.0
    purge = plan.initial_purge.resolved
    if purge is not None:
        dot = _dot_seconds(
            purge.amount_ul, plan.config.defaults.prime_extra_delay, motion, limits
        )
        stops += [
            (purge.point, capture),
            (purge.point + nozzle, dot),
            (purge.point, capture),
        ]
    flow = plan.flow_calibration
    if flow is not None:
        dot = _dot_seconds(flow.amount_ul, PRIME_EXTRA_DELAY, motion, limits)
        stops += [(point, capture) for point in flow.points]
        stops += [(point + nozzle, dot) for point in flow.points]
        stops += [(point, capture) for point in flow.points]
        wait = flow.settle_seconds
    return (
        _tour_seconds(
            [position for position, _ in stops], limits.velocity, limits.accel
        )
        + sum(seconds for _, seconds in stops)
        + wait
    )


def _dot_seconds(
    amount_ul: float,
    prime_extra_delay: float,
    motion: DispenseMotion,
    limits: MotionLimits,
) -> float:
    """点塗布（``PasteApplicator.deposit_at``）1 回の時間（移動を除く）."""
    sequence = FillSequence(
        path=Path((Point3d(0.0, 0.0, 0.0),)),
        total_amount_ul=amount_ul,
        motion=motion,
        prime_extra_delay=prime_extra_delay,
    )
    return _fill_seconds(sequence, motion, limits)


def _tour_seconds(points: Sequence[Point2d], velocity: float, accel: float) -> float:
    """``points`` を順に、1 か所ごとに止まりながら回る移動の時間."""
    return sum(
        _move_seconds((end - start).norm, velocity, accel)
        for start, end in zip(points, points[1:], strict=False)
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
