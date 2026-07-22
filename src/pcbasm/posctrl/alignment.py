"""関心領域(ROI)単位pad位置合わせの配線と補正結果のlookup."""

import logging
import math
from collections.abc import Sequence
from typing import Self

import attrs

from pcbasm.geometry import Compose, Point2d, Transform, sort_by_nearest
from pcbasm.pcb import Layer, Pad
from pcbasm.posctrl.copper import CopperEdgeMatcher, CopperProjector
from pcbasm.posctrl.pad import (
    PadAligner,
    PadAlignmentResult,
    PadRegion,
    plan_pad_regions,
)
from pcbasm.posctrl.setup import BoardCalibrationResult
from pcbasm.vision import CopperEdgeDetector, FrameSink

logger = logging.getLogger(__name__)


def _region_size(result: BoardCalibrationResult) -> tuple[float, float]:
    """crop÷pixel_per_mmから領域サイズ [mm] を導出する.

    crop（``machine.camera.crop``）はレンズ歪みの起こらない信頼範囲を
    捉えた設定であり、位置合わせのROIもその範囲に収めるのが本質という
    運用意図（計画書「設計（確定）」節）による。
    """
    crop_width, crop_height = result.machine.camera.crop.size
    pixel_per_mm = result.calibration.pixel_per_mm
    return (crop_width / pixel_per_mm, crop_height / pixel_per_mm)


def sorted_top_pad_regions(
    result: BoardCalibrationResult, pads: Sequence[Pad] | None = None
) -> list[PadRegion]:
    """TOP層のpadを関心領域(ROI)単位にまとめ、現在位置からの巡回順で返す.

    領域サイズはcrop÷pixel_per_mmから導出する（``_region_size``）。

    Args:
        result: ボードキャリブレーション結果
        pads: 対象pad列。省略時は ``result.pcb.pads`` 全体

    Returns:
        padを1つ以上持つTOP層領域（領域中心をboard_transformで機械座標化
        してから、現在のstage位置を起点にしたnearest neighbor + 2-opt
        巡回順で返す）
    """
    source = pads if pads is not None else result.pcb.pads
    top_pads = [p for p in source if p.layer == Layer.TOP]
    regions = plan_pad_regions(top_pads, _region_size(result))

    board_transform = result.board_transform
    start = result.stage.get_position().to2d().to3d()
    return sort_by_nearest(
        regions,
        start,
        key=lambda region: board_transform.apply(region.center).to3d(),
    )


@attrs.frozen
class RegionAlignments:
    """領域ごとのpad位置合わせ結果のlookup.

    Attributes:
        board_transform: board座標→機械座標の変換
        results: (領域, 位置合わせ結果) の列
    """

    board_transform: Transform
    results: tuple[tuple[PadRegion, PadAlignmentResult], ...]

    def result_for(self, pad: Pad) -> PadAlignmentResult | None:
        """padが属する領域の位置合わせ結果を返す.

        Padのattrs同値比較で所属領域を線形探索する
        （``(designator, pad_number)`` はKiCAD上一意でないため使わない）。

        Args:
            pad: 対象pad

        Returns:
            位置合わせ結果。未登録の場合はNone
        """
        for region, alignment in self.results:
            if pad in region.pads:
                return alignment
        return None

    def board_correction(self, pad: Pad) -> Transform | None:
        """Board座標空間での補正変換 C = T_b⁻¹∘M∘T_b を返す.

        Args:
            pad: 対象pad

        Returns:
            board座標の点を補正済みのboard座標へ写すTransform。
            未登録の場合はNone
        """
        result = self.result_for(pad)
        if result is None:
            return None
        return Compose(
            [
                self.board_transform,
                result.machine_transform,
                self.board_transform.inverse(),
            ]
        )


def _rotation_radians(transform: Transform) -> float:
    """変換の実回転角（ラジアン）をx単位ベクトルの像から導出する.

    board_transformはほぼ剛体（回転+並進+一様スケール）という前提のもと、
    並進成分を打ち消したx軸単位ベクトルの写像先から回転角を近似する。
    """
    origin = transform.apply(Point2d(0.0, 0.0))
    unit_x = transform.apply(Point2d(1.0, 0.0))
    direction = unit_x - origin
    return math.atan2(direction.y, direction.x)


def _validate_region_fits_frame(
    *,
    region_size: tuple[float, float],
    board_transform: Transform,
    image_size: tuple[int, int],
    pixel_per_mm: float,
    roi_margin: float,
    search_window: float,
) -> None:
    """crop由来の領域サイズが、board_transformの回転を考慮しても視野に 収まることを検証する.

    領域サイズ(w, h)をθ回転した外接矩形の幅・高さ
    ``w×|cosθ| + h×|sinθ|`` / ``w×|sinθ| + h×|cosθ|``
    （θ=board_transformの実回転）に、それぞれ ``2×(roi_margin+search_window)``
    を加えたものがFOVに収まることを検証する（計画書「設計（確定）」節）。

    Raises:
        ValueError: 収まらない場合
    """
    theta = _rotation_radians(board_transform)
    cos_theta = abs(math.cos(theta))
    sin_theta = abs(math.sin(theta))
    inset = 2.0 * (roi_margin + search_window)
    fov_width = image_size[0] / pixel_per_mm
    fov_height = image_size[1] / pixel_per_mm
    width, height = region_size
    required_width = width * cos_theta + height * sin_theta + inset
    required_height = width * sin_theta + height * cos_theta + inset

    if required_width > fov_width or required_height > fov_height:
        raise ValueError(
            f"pad_align領域サイズ {region_size[0]:.2f}x{region_size[1]:.2f} mm"
            f"（board回転 {math.degrees(theta):+.2f} deg 考慮で "
            f"{required_width:.2f}x{required_height:.2f} mm 必要）が視野 "
            f"{fov_width:.2f}x{fov_height:.2f} mm に収まりません。"
            "camera.crop を縮小するか、pad_align の"
            "roi_margin/search_window を調整してください"
        )


class PadAlignmentSession:
    """TOP層銅箔照合によるpad位置合わせの配線をまとめたセッション.

    BoardCalibrationResultからCopperProjector / CopperEdgeMatcher /
    CopperEdgeDetector / PadAlignerを構築し、関心領域(ROI)単位の位置合わせと
    補正済み投影器の生成を提供する。
    """

    def __init__(
        self, result: BoardCalibrationResult, frame_sink: FrameSink | None = None
    ) -> None:
        """PadAlignmentSessionを初期化する.

        Args:
            result: ボードキャリブレーション結果
            frame_sink: 照合状況フレームを送る sink。Noneの場合は表示しない

        Raises:
            ValueError: crop由来の領域サイズが視野に収まらない場合
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

        _validate_region_fits_frame(
            region_size=_region_size(result),
            board_transform=self._board_transform,
            image_size=self._image_size,
            pixel_per_mm=self._pixel_per_mm,
            roi_margin=pad_align.roi_margin,
            search_window=pad_align.search_window,
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
            image_size=self._image_size,
            search_window_px=round(pad_align.search_window * self._pixel_per_mm),
            roi_margin_mm=pad_align.roi_margin,
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

    def align(self, target: PadRegion) -> PadAlignmentResult | None:
        """領域単位の位置合わせを実行し、失敗時はNoneを返す.

        Args:
            target: 対象領域

        Returns:
            位置合わせ結果。照合失敗・非収束の場合はNone

        Raises:
            ValueError: ROIがフレームに収まらない等の設定エラーの場合
        """
        try:
            return self._aligner.align(target)
        except RuntimeError as exc:
            logger.warning("領域 %s の照合に失敗: %s", target.label, exc)
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
