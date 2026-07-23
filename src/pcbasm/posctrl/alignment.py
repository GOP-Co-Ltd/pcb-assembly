"""銅箔pad位置合わせの候補選定、配線、補正結果."""

import logging
import math
from collections.abc import Sequence
from typing import Self

import attrs

from pcbasm.geometry import Compose, Point2d, Shift, Transform, sort_by_nearest
from pcbasm.pcb import Layer, Pad
from pcbasm.posctrl.copper import CopperEdgeMatcher, CopperProjector
from pcbasm.posctrl.pad import (
    ComponentPads,
    PadAligner,
    PadAlignmentResult,
    PadAlignmentTarget,
    group_pads_by_component,
)
from pcbasm.posctrl.setup import BoardCalibrationResult
from pcbasm.vision import CopperEdgeDetector, FrameSink

logger = logging.getLogger(__name__)


@attrs.frozen
class PadAlignmentCandidates:
    """安全距離を満たすpad位置合わせ候補と選定集計."""

    targets: tuple[PadAlignmentTarget, ...]
    preferred_component_count: int
    rejected_count: int


def rank_safe_pad_alignment_targets(
    targets: Sequence[PadAlignmentTarget],
    *,
    max_correction_mm: float,
    tolerance_mm: float,
) -> PadAlignmentCandidates:
    """安全なTOP銅箔padをComponent分散優先・面積順で返す.

    対象padと他候補の実銅箔重心距離が
    ``2 * max_correction_mm + tolerance_mm`` より大きいものだけを採用する。
    各designatorの最小面積padを先に並べ、その後へ残りを面積順で続ける。
    """
    indexed: list[tuple[int, PadAlignmentTarget]] = []
    for index, target in enumerate(targets):
        polygon = target.pad.copper_polygon
        if (
            target.pad.layer != Layer.TOP
            or not polygon.is_valid
            or polygon.is_empty
            or polygon.area <= 0
        ):
            continue
        indexed.append((index, target))

    minimum_distance = 2 * max_correction_mm + tolerance_mm
    safe: list[tuple[int, PadAlignmentTarget]] = []
    for index, target in indexed:
        position = target.position
        if all(
            (position - other.position).norm > minimum_distance
            for other_index, other in indexed
            if other_index != index
        ):
            safe.append((index, target))

    def key(item: tuple[int, PadAlignmentTarget]) -> tuple:
        index, target = item
        position = target.position
        return (
            target.pad.copper_polygon.area,
            target.pad.designator,
            target.pad.pad_number,
            position.x,
            position.y,
            index,
        )

    ordered = sorted(safe, key=key)
    preferred: list[tuple[int, PadAlignmentTarget]] = []
    remaining: list[tuple[int, PadAlignmentTarget]] = []
    seen_designators: set[str] = set()
    for item in ordered:
        designator = item[1].pad.designator
        if designator not in seen_designators:
            preferred.append(item)
            seen_designators.add(designator)
        else:
            remaining.append(item)

    ranked = [target for _, target in (*preferred, *remaining)]
    return PadAlignmentCandidates(
        targets=tuple(ranked),
        preferred_component_count=len(preferred),
        rejected_count=len(targets) - len(ranked),
    )


def corrected_top_pad_entries(
    pads: Sequence[Pad],
    *,
    board_transform: Transform,
    machine_correction: Transform,
    start: Point2d,
) -> tuple[tuple[Pad, Point2d], ...]:
    """平均補正済みの全TOP pad巡回先をnearest neighbor順で返す."""
    entries = [
        (
            pad,
            machine_correction.apply(board_transform.apply(pad.center)),
        )
        for pad in pads
        if pad.layer == Layer.TOP
    ]
    return tuple(
        sort_by_nearest(
            entries,
            start.to3d(),
            key=lambda entry: entry[1].to3d(),
        )
    )


def sorted_top_component_pads(
    result: BoardCalibrationResult,
) -> list[ComponentPads]:
    """TOP層のpadを部品ごとにまとめ、現在位置からの巡回順で返す.

    Args:
        result: ボードキャリブレーション結果

    Returns:
        padを1つ以上持つTOP層部品のComponentPads
        （現在のstage位置を起点にしたnearest neighbor巡回順）
    """
    components = [c for c in result.pcb.components if c.layer == Layer.TOP]
    pads = [p for p in result.pcb.pads if p.layer == Layer.TOP]
    groups = group_pads_by_component(components, pads)
    start = result.stage.get_position().to2d().to3d()
    return sort_by_nearest(groups, start, key=lambda g: g.component.position.to3d())


@attrs.frozen
class ComponentAlignments:
    """部品ごとのpad位置合わせ結果のlookup.

    Attributes:
        board_transform: board座標→機械座標の変換
        results: (部品pad群, 位置合わせ結果) の列
    """

    board_transform: Transform
    results: tuple[tuple[ComponentPads, PadAlignmentResult], ...]

    def result_of(self, designator: str) -> PadAlignmentResult | None:
        """designatorに対応する位置合わせ結果を返す.

        Args:
            designator: 部品リファレンス

        Returns:
            位置合わせ結果。未登録の部品はNone
        """
        for group, alignment in self.results:
            if group.component.designator == designator:
                return alignment
        return None

    def corrected_board_transform(self, designator: str) -> Transform | None:
        """補正済みのboard座標→機械座標の変換を返す.

        Args:
            designator: 部品リファレンス

        Returns:
            ``Compose([board_transform, machine_transform])``。
            未登録の部品はNone
        """
        result = self.result_of(designator)
        if result is None:
            return None
        return Compose([self.board_transform, result.machine_transform])

    def board_correction(self, designator: str) -> Transform | None:
        """Board座標空間での補正変換 C = T_b⁻¹∘M∘T_b を返す.

        Args:
            designator: 部品リファレンス

        Returns:
            board座標の点を補正済みのboard座標へ写すTransform。
            未登録の部品はNone
        """
        result = self.result_of(designator)
        if result is None:
            return None
        return Compose(
            [
                self.board_transform,
                result.machine_transform,
                self.board_transform.inverse(),
            ]
        )


@attrs.frozen
class PadAlignments:
    """複数padの照合成功結果から算出する基板共通のXY補正.

    個別照合の回転成分は共通補正へ含めない。
    """

    board_transform: Transform
    results: tuple[tuple[PadAlignmentTarget, PadAlignmentResult], ...]

    def __attrs_post_init__(self) -> None:
        if not self.results:
            raise ValueError("pad位置合わせ結果がありません")

    def average_translation(self) -> Point2d:
        """成功結果のmachine空間XY並進を算術平均する."""
        count = len(self.results)
        return Point2d(
            math.fsum(result.translation.x for _, result in self.results) / count,
            math.fsum(result.translation.y for _, result in self.results) / count,
        )

    def averaged_machine_transform(self) -> Transform:
        """平均XY並進だけを適用するmachine空間Transformを返す."""
        translation = self.average_translation()
        return Shift(x=translation.x, y=translation.y)

    def averaged_board_correction(self) -> Transform:
        """平均並進をboard座標へ共役変換した補正を返す."""
        return Compose(
            [
                self.board_transform,
                self.averaged_machine_transform(),
                self.board_transform.inverse(),
            ]
        )


class PadAlignmentSession:
    """TOP層銅箔照合によるpad位置合わせの配線をまとめたセッション.

    BoardCalibrationResultからCopperProjector / CopperEdgeMatcher /
    CopperEdgeDetector / PadAlignerを構築し、単一padと既存の部品単位の
    位置合わせ、および補正済み投影器の生成を提供する。
    """

    def __init__(
        self, result: BoardCalibrationResult, frame_sink: FrameSink | None = None
    ) -> None:
        """PadAlignmentSessionを初期化する.

        Args:
            result: ボードキャリブレーション結果
            frame_sink: 照合状況フレームを送る sink。Noneの場合は表示しない
        """
        pad_align = result.machine.paste_dispenser.pad_align
        self._polygons = [c.polygon for c in result.pcb.copper if c.layer == Layer.TOP]
        self._board_transform = result.board_transform
        self._offset_transform = result.offset_transform
        self._pixel_per_mm = result.calibration.pixel_per_mm
        # 配線時にフレームを消費しないよう、キャリブレーション時の解像度を使う
        self._image_size = result.calibration.resolution
        self._projector = CopperProjector(
            polygons=self._polygons,
            board_transform=self._board_transform,
            offset_transform=self._offset_transform,
            pixel_per_mm=self._pixel_per_mm,
            image_size=self._image_size,
        )
        matcher = CopperEdgeMatcher(
            pixel_per_mm=self._pixel_per_mm,
            search_window_mm=pad_align.search_window,
            theta_range_degrees=pad_align.theta_range,
        )
        self._edge_detector = CopperEdgeDetector(
            canny_low=pad_align.canny_low,
            canny_high=pad_align.canny_high,
            blur_ksize=pad_align.blur_ksize,
        )
        self._aligner = PadAligner(
            camera=result.camera,
            klipper=result.klipper,
            stage=result.stage,
            projector=self._projector,
            matcher=matcher,
            edge_detector=self._edge_detector,
            board_transform=self._board_transform,
            offset_transform=self._offset_transform,
            roi_margin_mm=pad_align.roi_margin,
            min_roi_mm=pad_align.min_roi,
            tolerance=pad_align.tolerance,
            max_correction_mm=pad_align.max_correction,
            frame_sink=frame_sink,
        )

    @classmethod
    def from_calibration(
        cls, result: BoardCalibrationResult, frame_sink: FrameSink | None = None
    ) -> Self:
        """BoardCalibrationResultから配線済みセッションを構築する.

        Args:
            result: ボードキャリブレーション結果
            frame_sink: 照合状況フレームを送る sink。Noneの場合は表示しない

        Returns:
            配線済みのPadAlignmentSession
        """
        return cls(result, frame_sink=frame_sink)

    def align(self, target: ComponentPads) -> PadAlignmentResult | None:
        """部品単位の位置合わせを実行し、失敗時はNoneを返す.

        Args:
            target: 対象部品とそのpad群

        Returns:
            位置合わせ結果。照合失敗・非収束の場合はNone
        """
        try:
            return self._aligner.align(target)
        except RuntimeError as exc:
            logger.warning("部品 %s の照合に失敗: %s", target.component.designator, exc)
            return None

    def align_pad(self, target: PadAlignmentTarget) -> PadAlignmentResult | None:
        """単一padの位置合わせを実行し、失敗時はNoneを返す."""
        try:
            return self._aligner.align_pad(target)
        except RuntimeError as exc:
            logger.warning("%s の照合に失敗: %s", target.identifier, exc)
            return None

    def corrected_projector(self, machine_transform: Transform) -> CopperProjector:
        """board変換を補正済みに差し替えたCopperProjectorを生成する.

        Args:
            machine_transform: 位置合わせで得たmachine空間の補正Transform

        Returns:
            ``Compose([board_transform, machine_transform])`` で投影する
            CopperProjector
        """
        return CopperProjector(
            polygons=self._polygons,
            board_transform=Compose([self._board_transform, machine_transform]),
            offset_transform=self._offset_transform,
            pixel_per_mm=self._pixel_per_mm,
            image_size=self._image_size,
        )

    @property
    def projector(self) -> CopperProjector:
        """キャリブレーション時のboard変換による投影器（表示用）."""
        return self._projector

    @property
    def edge_detector(self) -> CopperEdgeDetector:
        """銅箔エッジ検出器（表示用）."""
        return self._edge_detector
