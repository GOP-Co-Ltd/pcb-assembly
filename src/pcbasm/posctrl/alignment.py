"""領域単位の銅箔照合の配線と、区ごとの局所補正."""

import logging
import statistics
from collections.abc import Sequence
from typing import Self

import attrs

from pcbasm.geometry import Compose, Point2d, Shift, Transform
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


@attrs.frozen
class BorrowedCorrections:
    """自区を持たない pad が、どれだけ離れた区の補正を借りたかの要約（診断用）.

    ROI 全体を基板外形の内側に収める条件で外周付近のタイルが落ちるため、
    全区が成功しても自分の区を持たない pad が残る。実銅箔の局所ずれには
    勾配があるので（実測 0.36mm / 3.3mm）、借用距離がそのまま誤差の上限になる。

    Attributes:
        pad_count: 対象 pad の総数
        borrowed_count: 自区を持たず、離れた区の補正を借りた pad の数
        median_distance: 借用した pad の借用距離の中央値 [mm]（借用0なら0.0）
        max_distance: 借用距離の最大 [mm]（借用0なら0.0）
    """

    pad_count: int
    borrowed_count: int
    median_distance: float
    max_distance: float


@attrs.frozen
class BoardAlignment:
    """成功した領域計測から pad ごとの局所補正を引くルックアップ.

    実銅箔は設計から**局所的に**ずれる（エッチングのレジストレーション誤差・
    基板の伸び・反り）。実測では隣接区の変位が 3.3mm 離れただけで 0.36mm 違い、
    大域アフィンでは残差 RMS 114um（照合ノイズの 20 倍以上）が残った。
    ペーストを乗せる相手は設計 pad ではなく実銅箔なので、平均や当てはめで
    情報を捨てず、pad が属する区の変位をそのまま使う。

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

    def correction_for(self, board_point: Point2d) -> Transform:
        """board座標の点に最も近い成功区の補正（純並進）を返す.

        区は pixel 空間の等サイズ正方格子タイルなので、最近傍の区中心を選ぶことが
        実質的に「その点を含む区を選ぶ」ことになる。含む区が無い（計画外・照合失敗）
        点にはそのまま最近傍の成功区が使われるので、包含判定とフォールバックを
        分ける必要はない。距離は board 座標で測る（同距離なら results の先頭）。

        board→pixel が相似写像でない（3点法の board 変換はスキューを持ち得る）と
        「最近傍 = 包含」は厳密には成り立たないが、外れるのは区境界のごく細い帯
        だけ。実測でスキュー 0.06° なら境界から 1.4um、1° でも 27.9um の帯であり、
        照合ノイズ（区あたり 5um 級）以下なので実用上は問題にならない。

        Args:
            board_point: 補正を引く点（board座標、mm）

        Returns:
            機械座標の補正Transform（Shift）
        """
        nearest = min(self.results, key=lambda r: self._distance(r, board_point))
        return Shift.from_point(nearest.displacement)

    def borrowed_corrections(
        self, board_points: Sequence[Point2d], *, region_size_mm: float
    ) -> BorrowedCorrections:
        """自区を持たない点が借りた補正の距離を集計する（ログ用の診断値）.

        「自区を持つ」は最近傍の成功区までの距離が区の半辺以下かどうかで判定する
        （等格子なので厳密な包含判定は要らない）。

        Args:
            board_points: 対象点（board座標、mm）。塗布なら pad 中心
            region_size_mm: 区の一辺を board 座標に直した長さ [mm]

        Returns:
            借用の件数と距離の要約
        """
        half = region_size_mm / 2
        distances = [
            distance
            for point in board_points
            if (distance := min(self._distance(r, point) for r in self.results)) > half
        ]
        return BorrowedCorrections(
            pad_count=len(board_points),
            borrowed_count=len(distances),
            median_distance=statistics.median(distances) if distances else 0.0,
            max_distance=max(distances, default=0.0),
        )

    @property
    def mean_displacement(self) -> Point2d:
        """区の変位の単純平均 [mm]（ログ用。補正には使わない）."""
        total = sum((r.displacement for r in self.results), Point2d(0.0, 0.0))
        return total / len(self.results)

    @property
    def displacement_spread(self) -> Point2d:
        """区の変位の軸ごとの母標準偏差 [mm]（ログ用。補正には使わない）."""
        return Point2d(
            x=statistics.pstdev(r.displacement.x for r in self.results),
            y=statistics.pstdev(r.displacement.y for r in self.results),
        )

    @staticmethod
    def _distance(result: RegionAlignment, board_point: Point2d) -> float:
        """区の中心から点までの距離 [mm]（board座標）."""
        return (result.region.board_center - board_point).norm


def corrected_board_transform(
    board_transform: Transform, alignment: BoardAlignment, board_point: Point2d
) -> Compose:
    """その点用の board 座標 → カメラ機械座標の変換（局所補正込み）を組む.

    補正は board 変換の**直後**に置く。照合で測った変位はカメラの機械座標系で
    定義されているので、この位置以外に挿すと意味が変わる。

    Args:
        board_transform: board座標→機械座標の変換（3点法の計測結果）
        alignment: 区ごとの局所補正
        board_point: 補正を引く点（board座標、mm）。塗布なら pad 中心

    Returns:
        board座標→カメラ機械座標の合成変換
    """
    return Compose([board_transform, alignment.correction_for(board_point)])


def corrected_pad_targets(
    board_transform: Transform, alignment: BoardAlignment, pads: Sequence[Pad]
) -> list[tuple[Pad, Point2d]]:
    """Pad ごとに局所補正を引き、(pad, 補正後のカメラ機械座標) を入力順で返す.

    補正を引く点は各 pad の中心なので、pad ごとに違う区の補正が当たる。対応を
    値として返すことで、「どの pad にどの補正を当てたか」が呼び出し側の
    ループの書き方に依存しなくなる。

    Args:
        board_transform: board座標→機械座標の変換（3点法の計測結果）
        alignment: 区ごとの局所補正
        pads: 対象pad

    Returns:
        入力 pads と同順・同数の (pad, 補正後のカメラ機械座標)
    """
    return [
        (
            pad,
            corrected_board_transform(board_transform, alignment, pad.center).apply(
                pad.center
            ),
        )
        for pad in pads
    ]


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
    def region_size_mm(self) -> float:
        """区の一辺を board 座標に直した長さ [mm]（借用距離の判定に使う）."""
        return self._pad_align.region_size_px / self._pixel_per_mm

    @property
    def region_roi(self) -> PixelRect:
        """全region共通の画像中心ROI（overlayの描画範囲に使う）."""
        return self._region_roi
