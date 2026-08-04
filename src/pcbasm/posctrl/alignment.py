"""領域単位の銅箔照合の配線とpad別の局所補正."""

import logging
import math
from collections.abc import Sequence

import attrs
from shapely import Polygon

from pcbasm.geometry import Point2d, Shift, Transform
from pcbasm.pcb import Layer, Pad
from pcbasm.posctrl.aligner import RegionAligner, RegionAlignment
from pcbasm.posctrl.copper import (
    CopperEdgeMatcher,
    CopperProjector,
    PixelRect,
    centered_roi,
)
from pcbasm.posctrl.region import AlignmentRegion, plan_alignment_regions
from pcbasm.posctrl.setup import BoardCalibrationResult
from pcbasm.vision import CopperEdgeDetector, FrameSink

logger = logging.getLogger(__name__)

_SHORT_SIDE_ABS_TOLERANCE_MM = 1e-9


def is_pad_refinement_target(
    pad: Pad,
    *,
    max_short_side_mm: float,
) -> bool:
    """paste開口の最小回転外接矩形の短辺が逐次位置合わせ対象か判定する."""
    if max_short_side_mm <= 0.0 or pad.polygon.is_empty or pad.polygon.area <= 0.0:
        return False

    rectangle = pad.polygon.minimum_rotated_rectangle
    if not isinstance(rectangle, Polygon) or rectangle.is_empty:
        return False
    coordinates = list(rectangle.exterior.coords)
    if len(coordinates) < 5:
        return False

    short_side = min(
        math.dist(coordinates[0], coordinates[1]),
        math.dist(coordinates[1], coordinates[2]),
    )
    return 0.0 < short_side <= max_short_side_mm + _SHORT_SIDE_ABS_TOLERANCE_MM


@attrs.frozen
class BoardAlignment:
    """成功した領域の変位からpad中心の補正を求める."""

    results: tuple[RegionAlignment, ...]
    fallback_results: tuple[RegionAlignment, ...] = ()

    def correction_for(
        self, board_point: Point2d, *, designator: str | None = None
    ) -> Transform:
        """点を覆う成功領域、なければ最近傍の成功領域から純並進を返す.

        Raises:
            ValueError: 成功領域が1件もない場合
        """
        available = self.results or self.fallback_results
        if not available:
            target = (
                designator
                if designator is not None
                else f"({board_point.x:.3f}, {board_point.y:.3f})"
            )
            raise ValueError(f"{target} の補正に使える位置合わせ成功領域がありません")
        covering = [
            result for result in self.results if result.region.covers(board_point)
        ]
        if not covering and self.fallback_results:
            return BoardAlignment(self.fallback_results).correction_for(
                board_point, designator=designator
            )
        if not covering:
            nearest = min(
                available,
                key=lambda result: (
                    (result.region.board_center - board_point).norm,
                    result.match.rms_distance_px,
                ),
            )
            return Shift.from_point(nearest.displacement)
        selected = min(
            covering,
            key=lambda result: (
                sum(
                    (result.displacement - other.displacement).norm
                    for other in covering
                ),
                result.match.rms_distance_px,
                (result.region.board_center - board_point).norm,
            ),
        )
        return Shift.from_point(selected.displacement)


class RegionAlignmentSession:
    """TOP層銅箔の領域計画と照合依存をまとめるセッション."""

    def __init__(
        self, result: BoardCalibrationResult, frame_sink: FrameSink | None = None
    ) -> None:
        """BoardCalibrationResultから照合器を配線する."""
        pad_align = result.machine.paste_dispenser.pad_align
        self._pcb = result.pcb
        self._stage = result.stage
        self._pad_align = pad_align
        self._board_transform = result.board_transform
        self._image_size = result.calibration.resolution
        self._projector = CopperProjector(
            polygons=[
                copper.polygon
                for copper in result.pcb.copper
                if copper.layer == Layer.TOP
            ],
            board_transform=result.board_transform,
            offset_transform=result.offset_transform,
            pixel_per_mm=result.calibration.pixel_per_mm,
            image_size=self._image_size,
        )
        matcher = CopperEdgeMatcher(
            pixel_per_mm=result.calibration.pixel_per_mm,
            search_window_mm=pad_align.search_window,
        )
        required = pad_align.region_size_px + 2 * matcher.window_px
        if required > min(self._image_size):
            raise ValueError(
                f"region_size_px {pad_align.region_size_px} px + 探索窓 "
                f"{matcher.window_px} px x2 = {required} px が"
                f"キャリブレーション解像度 {self._image_size} に収まりません"
            )
        self._edge_detector = CopperEdgeDetector(
            canny_low=pad_align.canny_low,
            canny_high=pad_align.canny_high,
            blur_ksize=pad_align.blur_ksize,
        )
        self._aligner = RegionAligner(
            camera=result.camera,
            klipper=result.klipper,
            stage=result.stage,
            projector=self._projector,
            matcher=matcher,
            edge_detector=self._edge_detector,
            offset_transform=result.offset_transform,
            max_correction_mm=pad_align.max_correction,
            max_passes=pad_align.max_passes,
            converge_tolerance_mm=pad_align.converge_tolerance,
            frame_sink=frame_sink,
        )

    def plan_regions(self, pad_centers: Sequence[Point2d]) -> list[AlignmentRegion]:
        """塗布対象pad中心を覆う照合領域を計画する."""
        return plan_alignment_regions(
            self._projector,
            self._board_transform,
            pad_centers,
            safe_area=self._pcb.outline.polygon.buffer(
                -self._pad_align.board_edge_margin
            ),
            region_size_px=self._pad_align.region_size_px,
            overlap=self._pad_align.region_overlap,
            image_size=self._image_size,
            tour_start=self._stage.get_position().to2d(),
        )

    def align(self, region: AlignmentRegion) -> RegionAlignment | None:
        """領域を照合し、失敗または非収束ならNoneを返す."""
        try:
            return self._aligner.measure(region)
        except RuntimeError as exc:
            logger.warning("領域 %d の照合に失敗: %s", region.index, exc)
            return None

    def refine(
        self,
        board_point: Point2d,
        initial_correction: Transform,
        board_area: Polygon,
    ) -> RegionAlignment | None:
        """領域補正を初期値に、pad中心で銅箔照合を収束させる."""
        anchor = self._board_transform.apply(board_point)
        initial_displacement = initial_correction.apply(anchor) - anchor
        region = AlignmentRegion(
            index=-1,
            board_center=board_point,
            anchor=anchor,
            roi=self.region_roi,
            board_area=board_area,
        )
        try:
            return self._aligner.measure(
                region, initial_displacement=initial_displacement
            )
        except RuntimeError as exc:
            logger.warning(
                "pad中心 (%.3f, %.3f) の再照合に失敗: %s",
                board_point.x,
                board_point.y,
                exc,
            )
            return None

    @property
    def projector(self) -> CopperProjector:
        """キャリブレーション時のboard変換による投影器."""
        return self._projector

    @property
    def edge_detector(self) -> CopperEdgeDetector:
        """overlay表示に使う銅箔エッジ検出器."""
        return self._edge_detector

    @property
    def region_roi(self) -> PixelRect:
        """全領域共通の画像中心ROI."""
        return centered_roi(self._image_size, self._pad_align.region_size_px)
