"""領域単位の銅箔照合の配線と、基板全体の平均並進補正."""

import logging
from typing import Self

import attrs
import numpy as np

from pcbasm.geometry import Compose, Point2d, Shift, Transform
from pcbasm.pcb import Layer
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


@attrs.frozen
class BoardAlignment:
    """複数領域の計測から得た基板全体の平均並進補正.

    Attributes:
        results: 成功した領域計測（1件以上）
    """

    results: tuple[RegionAlignment, ...]

    def __attrs_post_init__(self) -> None:
        """結果が空でないことを検証する.

        Raises:
            ValueError: resultsが空の場合
        """
        if not self.results:
            raise ValueError("BoardAlignmentには1件以上の領域計測が必要です")

    @property
    def translation(self) -> Point2d:
        """Machine空間の平均並進 [mm]."""
        translations = [r.translation for r in self.results]
        return Point2d(
            x=float(np.mean([t.x for t in translations])),
            y=float(np.mean([t.y for t in translations])),
        )

    @property
    def machine_transform(self) -> Transform:
        """平均並進のTransform."""
        return Shift.from_point(self.translation)

    @property
    def spread(self) -> Point2d:
        """領域間の標準偏差 [mm]（母標準偏差 ddof=0。1領域なら (0, 0)）."""
        translations = [r.translation for r in self.results]
        return Point2d(
            x=float(np.std([t.x for t in translations])),
            y=float(np.std([t.y for t in translations])),
        )


class RegionAlignmentSession:
    """TOP層銅箔照合による領域位置合わせの配線をまとめたセッション.

    BoardCalibrationResultからCopperProjector / CopperEdgeMatcher /
    CopperEdgeDetector / RegionAlignerを構築し、領域の計画・計測と 補正済み投影器の生成を提供する。
    """

    def __init__(
        self, result: BoardCalibrationResult, frame_sink: FrameSink | None = None
    ) -> None:
        """RegionAlignmentSessionを初期化する.

        Args:
            result: ボードキャリブレーション結果
            frame_sink: 照合状況フレームを送る sink。Noneの場合は表示しない

        Raises:
            ValueError: region_size_px + 2 * window_px がキャリブレーション
                解像度に収まらない場合
        """
        pad_align = result.machine.paste_dispenser.pad_align
        self._pcb = result.pcb
        self._stage = result.stage
        self._pad_align = pad_align
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
            min_sharpness=pad_align.min_sharpness,
        )
        required = pad_align.region_size_px + 2 * matcher.window_px
        if required > min(self._image_size):
            raise ValueError(
                f"region_size_px {pad_align.region_size_px} px + 探索窓 "
                f"{matcher.window_px} px x2 = {required} px が"
                f"キャリブレーション解像度 {self._image_size} に収まりません"
            )
        self._region_roi = centered_roi(self._image_size, pad_align.region_size_px)
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
            offset_transform=self._offset_transform,
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
            配線済みのRegionAlignmentSession
        """
        return cls(result, frame_sink=frame_sink)

    def plan_regions(self) -> list[AlignmentRegion]:
        """基板外形の内側から照合領域を計画する（撮像・移動なし）.

        巡回起点は呼び出し時の ``stage.get_position()``。照合を許す領域は基板外形を
        ``board_edge_margin`` [mm] 縮めたもので、ROI 全体がその内側に収まる位置しか
        候補にならない。外周はやすり掛けで銅箔が削れやすく、かつ基板外形線が
        想定エッジに含まれない偽エッジとして働くため。

        Returns:
            巡回順の照合領域。外形を縮めた領域が空なら空リスト
        """
        return plan_alignment_regions(
            self._projector,
            self._board_transform,
            safe_area=self._pcb.outline.polygon.buffer(
                -self._pad_align.board_edge_margin
            ),
            region_size_px=self._pad_align.region_size_px,
            count=self._pad_align.region_count,
            image_size=self._image_size,
            tour_start=self._stage.get_position().to2d(),
        )

    def measure(self, region: AlignmentRegion) -> RegionAlignment | None:
        """1領域を計測し、失敗時は警告logの後Noneを返す.

        Args:
            region: 対象領域

        Returns:
            領域の照合結果。照合失敗の場合はNone
        """
        try:
            return self._aligner.measure(region)
        except RuntimeError as exc:
            logger.warning("領域 %d の照合に失敗: %s", region.index, exc)
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

    @property
    def region_roi(self) -> PixelRect:
        """全region共通の画像中心ROI（overlayの描画範囲に使う）."""
        return self._region_roi
