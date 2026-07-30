"""領域単位の銅箔照合の配線と、基板全体のアフィン補正."""

import logging
import math
from collections.abc import Sequence
from typing import Literal, Self

import attrs
import numpy as np

from pcbasm.geometry import Compose, Matrix2d, Point2d, Shift, Transform
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

type DisplacementModel = Literal["affine", "translation"]

# アンカーの最小主軸方向 RMS 広がりの下限 [mm]。これ未満はアフィンを諦める。
# 実測: spread 1.5mm 付近で並進のみに負け、0.16mm では p95 853um まで暴れる
_MIN_ANCHOR_SPREAD_MM = 2.0


@attrs.frozen
class DisplacementFit:
    """機械座標の変位場 d(p) = L (p − c) + t のアフィン最小二乗当てはめ.

    Attributes:
        model: 使用した模型。アンカーが3点未満または準共線なら "translation"
        centroid: アンカーの重心 c（機械座標、mm）
        translation: 重心での変位 t = mean(d_i)（mm）
        anchor_spread_mm: アンカーの最小主軸方向 RMS 広がり [mm]
        machine_transform: 補正 p ↦ p + d(p)
    """

    model: DisplacementModel
    centroid: Point2d
    translation: Point2d
    anchor_spread_mm: float
    machine_transform: Transform

    def predict(self, machine_point: Point2d) -> Point2d:
        """その点での変位 d(p) を返す（= machine_transform.apply(p) − p）."""
        return self.machine_transform.apply(machine_point) - machine_point


def fit_displacement(
    anchors: Sequence[Point2d], displacements: Sequence[Point2d]
) -> DisplacementFit:
    """区ごとの (アンカー, 変位) からアフィン変位場を最小二乗で当てはめる.

    ``d(p) = L (p − c) + t`` を最小二乗すると ``Σ (p_i − c) = 0`` から t と L が
    分離し、``t = mean(d_i)``・``L`` は重心化した連立の最小二乗解になる。
    最小主軸方向の広がりが ``_MIN_ANCHOR_SPREAD_MM`` 未満ならレバー腕が
    信頼できないので並進のみへ縮退する（2点以下は広がり0なので必ず縮退する）。

    Args:
        anchors: 区のアンカー（機械座標、mm）
        displacements: 各アンカーで測った変位（mm、anchorsと同順・同数）

    Returns:
        当てはめ結果

    Raises:
        ValueError: anchorsが空、または長さがdisplacementsと違う場合
    """
    if not anchors:
        raise ValueError("fit_displacementには1件以上のアンカーが必要です")
    if len(anchors) != len(displacements):
        raise ValueError(
            f"anchorsとdisplacementsの長さが違います: "
            f"{len(anchors)} != {len(displacements)}"
        )

    positions = np.array([[a.x, a.y] for a in anchors])
    observed = np.array([[d.x, d.y] for d in displacements])
    centroid = positions.mean(axis=0)
    centered = positions - centroid
    translation = observed.mean(axis=0)
    # 最小特異値がそのまま最小主軸方向の広がり。2点以下は必ず0になる
    spread = float(np.linalg.svd(centered, compute_uv=False)[-1]) / math.sqrt(
        len(anchors)
    )

    model: DisplacementModel
    if spread < _MIN_ANCHOR_SPREAD_MM:
        model = "translation"
        linear = np.zeros((2, 2))
    else:
        model = "affine"
        linear = np.linalg.lstsq(centered, observed - translation, rcond=None)[0].T

    # p ↦ p + d(p) = (I + L)(p − c) + (c + t)
    machine_transform = Compose(
        [
            Shift(x=-float(centroid[0]), y=-float(centroid[1])),
            Matrix2d(np.eye(2) + linear),
            Shift(
                x=float(centroid[0] + translation[0]),
                y=float(centroid[1] + translation[1]),
            ),
        ]
    )
    return DisplacementFit(
        model=model,
        centroid=Point2d(x=float(centroid[0]), y=float(centroid[1])),
        translation=Point2d(x=float(translation[0]), y=float(translation[1])),
        anchor_spread_mm=spread,
        machine_transform=machine_transform,
    )


@attrs.frozen
class BoardAlignment:
    """複数領域の計測から得た基板全体のアフィン補正.

    Attributes:
        results: 成功した領域計測（1件以上）
        fit: resultsから当てはめた変位場
    """

    results: tuple[RegionAlignment, ...]
    fit: DisplacementFit = attrs.field(init=False, eq=False)

    def __attrs_post_init__(self) -> None:
        """結果が空でないことを検証し、変位場を当てはめる.

        Raises:
            ValueError: resultsが空の場合
        """
        if not self.results:
            raise ValueError("BoardAlignmentには1件以上の領域計測が必要です")
        object.__setattr__(
            self,
            "fit",
            fit_displacement(
                [r.region.anchor for r in self.results],
                [r.displacement for r in self.results],
            ),
        )

    @property
    def machine_transform(self) -> Transform:
        """補正Transform（fit.machine_transform）."""
        return self.fit.machine_transform

    @property
    def model(self) -> DisplacementModel:
        """使用した模型（"affine" / "translation"）."""
        return self.fit.model

    @property
    def translation(self) -> Point2d:
        """重心での変位 [mm]（ログ・summary 用）."""
        return self.fit.translation

    @property
    def residuals(self) -> tuple[Point2d, ...]:
        """区ごとの残差 r_i = d_i − d̂(anchor_i) [mm]（resultsと同順）."""
        return tuple(
            r.displacement - self.fit.predict(r.region.anchor) for r in self.results
        )

    @property
    def residual_rms(self) -> float:
        """残差のRMSノルム [mm] = sqrt(mean(|r_i|²))."""
        residuals = self.residuals
        return math.sqrt(sum(r.norm**2 for r in residuals) / len(residuals))

    @property
    def residual_max(self) -> float:
        """残差ノルムの最大 [mm]."""
        return max(r.norm for r in self.residuals)


class RegionAlignmentSession:
    """TOP層銅箔照合による領域位置合わせの配線をまとめたセッション.

    BoardCalibrationResultからCopperProjector / CopperEdgeMatcher /
    CopperEdgeDetector / RegionAlignerを構築し、領域の計画と計測を提供する。
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
            max_passes=pad_align.max_passes,
            converge_tolerance_mm=pad_align.converge_tolerance,
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

    def plan_regions(self, pad_centers: Sequence[Point2d]) -> list[AlignmentRegion]:
        """塗布対象padの分布から照合領域を計画する（撮像・移動なし）.

        巡回起点は呼び出し時の ``stage.get_position()``。照合を許す領域は基板外形を
        ``board_edge_margin`` [mm] 縮めたもので、ROI 全体がその内側に収まる位置しか
        候補にならない。外周はやすり掛けで銅箔が削れやすく、かつ基板外形線が
        想定エッジに含まれない偽エッジとして働くため。

        Args:
            pad_centers: 塗布対象padの中心（board座標、mm）。区内にこれが
                1つも無い区はスキップする

        Returns:
            巡回順の照合領域。条件を満たす区が無ければ空リスト
        """
        return plan_alignment_regions(
            self._projector,
            self._board_transform,
            pad_centers,
            safe_area=self._pcb.outline.polygon.buffer(
                -self._pad_align.board_edge_margin
            ),
            region_size_px=self._pad_align.region_size_px,
            min_sharpness=self._pad_align.min_sharpness,
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
