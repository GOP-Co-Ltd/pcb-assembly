"""位置合わせの本筋（領域照合 → pad 中心の精密照合 → 補正の選択）.

部品は :func:`align_regions`、:func:`refine_pad`、:class:`BoardCorrection` で、塗布の前準備はそれを合成した :func:`align_board` を呼ぶ。
Board 巡回は巡回と精密照合を交互に行うので、部品を組んで使う。

経過はイベント（:data:`AlignEvent`）で呼び出し側へ渡す。照合ログの文字列はイベントが持つ。
"""

import logging
from collections.abc import Callable, Sequence

import attrs
from shapely import Polygon

from pcbasm.align.aligner import RegionAligner, RegionFit
from pcbasm.align.board_frame import BoardFrame
from pcbasm.align.copper import CopperEdgeMatcher, CopperProjector
from pcbasm.align.region import AlignmentRegion, plan_alignment_regions
from pcbasm.board import Layer, Pad
from pcbasm.config import MachineConfig
from pcbasm.geometry import Point2d, Shift, Transform, oriented_bbox
from pcbasm.hal import Camera, KlipperClient, KlipperError, XYZStage
from pcbasm.vision import CopperEdgeDetector, FrameSink, PixelRect

logger = logging.getLogger(__name__)

_SHORT_SIDE_ABS_TOLERANCE_MM = 1e-9


def refinement_targets(
    pads: Sequence[Pad], *, max_short_side_mm: float
) -> tuple[Pad, ...]:
    """Paste 開口の最小回転外接矩形の短辺が ``max_short_side_mm`` 以下の pad（pad 中心で精密照合する対象）を返す.

    ``max_short_side_mm`` が 0 以下なら対象は無い。
    """
    if max_short_side_mm <= 0.0:
        return ()
    limit = max_short_side_mm + _SHORT_SIDE_ABS_TOLERANCE_MM
    return tuple(
        pad
        for pad in pads
        if (box := oriented_bbox(pad.polygon)) is not None
        and 0.0 < box.short_length <= limit
    )


class RegionAlignmentSession:
    """TOP 層銅箔の領域計画と照合の依存オブジェクトをまとめるセッション."""

    def __init__(
        self,
        board_frame: BoardFrame,
        machine: MachineConfig,
        *,
        klipper: KlipperClient,
        stage: XYZStage,
        camera: Camera,
        frame_sink: FrameSink | None = None,
    ) -> None:
        """基板の位置関係と装置から照合器を組み立てる."""
        alignment = machine.alignment
        self._pcb = board_frame.board
        self._stage = stage
        self._alignment = alignment
        self._board_transform = board_frame.board_transform
        camera_model = board_frame.camera_model
        self._projector = CopperProjector.from_board_frame(board_frame, layer=Layer.TOP)
        matcher = CopperEdgeMatcher(
            pixel_per_mm=camera_model.pixel_per_mm,
            search_window_mm=alignment.search_window,
        )
        required = alignment.region_size_px + 2 * matcher.window_px
        if required > min(camera_model.image_size):
            raise ValueError(
                f"region_size_px {alignment.region_size_px} px + 探索窓 "
                f"{matcher.window_px} px x2 = {required} px が"
                f"キャリブレーション解像度 {camera_model.image_size} に収まりません"
            )
        self._region_roi = camera_model.centered_roi(alignment.region_size_px)
        self._edge_detector = CopperEdgeDetector(
            canny_low=alignment.canny_low,
            canny_high=alignment.canny_high,
            blur_ksize=alignment.blur_ksize,
        )
        self._aligner = RegionAligner(
            camera=camera,
            camera_model=camera_model,
            klipper=klipper,
            stage=stage,
            projector=self._projector,
            matcher=matcher,
            edge_detector=self._edge_detector,
            max_correction_mm=alignment.max_correction,
            max_passes=alignment.max_passes,
            converge_tolerance_mm=alignment.converge_tolerance,
            settle_sec=machine.settle.move_sec,
            frame_sink=frame_sink,
        )

    def plan_regions(self, pad_centers: Sequence[Point2d]) -> list[AlignmentRegion]:
        """塗布対象 pad の中心（board 座標）を覆う照合領域を計画する.

        巡回順は現在のステージ位置から近い順に決める。
        """
        return plan_alignment_regions(
            self._projector,
            self._board_transform,
            pad_centers,
            safe_area=self._pcb.outline.polygon.buffer(
                -self._alignment.board_edge_margin
            ),
            roi=self._region_roi,
            overlap=self._alignment.region_overlap,
            tour_start=self._stage.get_position().to2d(),
        )

    def align(self, region: AlignmentRegion) -> RegionFit | None:
        """領域を照合し、失敗または非収束なら None を返す.

        Raises:
            KlipperError: Klipper が移動を断った場合（領域の失敗にせず、ジョブを止める）
        """
        try:
            return self._aligner.measure(region)
        except KlipperError:
            raise
        except RuntimeError as exc:
            logger.warning("領域 %d の照合に失敗: %s", region.index, exc)
            return None

    def refine(
        self,
        board_point: Point2d,
        initial_correction: Transform,
        board_area: Polygon,
    ) -> RegionFit | None:
        """領域補正を初期値にして、pad 中心で銅箔照合を収束させる.

        Args:
            board_point: pad 中心（board 座標）
            initial_correction: 初期値にする補正（``BoardCorrection.correction_for`` の結果）
            board_area: 結果が覆う範囲（board 座標）。通常は pad のポリゴン

        Returns:
            収束した結果。失敗または非収束なら None

        Raises:
            KlipperError: Klipper が移動を断った場合
        """
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
        except KlipperError:
            raise
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
        """キャリブレーション時の board 変換による投影器."""
        return self._projector

    @property
    def edge_detector(self) -> CopperEdgeDetector:
        """Overlay 表示に使う銅箔エッジ検出器."""
        return self._edge_detector

    @property
    def region_roi(self) -> PixelRect:
        """全領域共通の画像中心 ROI."""
        return self._region_roi


@attrs.frozen
class PadRefinement:
    """1 pad の中心精密照合の結果.

    Attributes:
        pad: 対象 pad
        result: 収束した照合結果（収束しなければ ``None`` = 領域補正を使う）
        residual: 領域補正に対する残差 [mm]（収束しなければ ``None``）
    """

    pad: Pad
    result: RegionFit | None
    residual: Point2d | None


@attrs.frozen
class BoardCorrection:
    """位置合わせの結果から、board 上の点ごとの補正を選ぶ.

    成功した領域が 1 件も無ければ作れない（どの点の補正も求まらないので、塗布も巡回も始めない）。

    Attributes:
        fits: 収束した領域の照合結果
        region_count: 照合した領域の数（失敗を含む）
        refinements: pad 中心の精密照合の結果（収束しなかったものを含む）

    Raises:
        ValueError: ``fits`` が空の場合
    """

    fits: tuple[RegionFit, ...]
    region_count: int
    refinements: tuple[PadRefinement, ...] = ()

    def __attrs_post_init__(self) -> None:
        if not self.fits:
            raise ValueError(
                f"位置合わせに成功した領域がありません（照合 {self.region_count} 領域）"
            )

    def correction_for(self, board_point: Point2d) -> Transform:
        """Board 座標の点に使う補正（機械座標の純並進）を返す.

        返り値は ``board_transform.apply(board_point)`` に適用する。

        次の順に選ぶ。

        1. 点を覆う精密照合の結果があれば、その中で変位が他と最も揃うもの
        2. 無ければ、点を覆う領域の結果から 1 と同じ基準で選ぶ
        3. どちらも無ければ、領域中心が点に最も近い領域の結果
        """
        refined = [r.result for r in self.refinements if r.result is not None]
        covering = [fit for fit in refined if fit.region.covers(board_point)] or [
            fit for fit in self.fits if fit.region.covers(board_point)
        ]
        if not covering:
            nearest = min(
                self.fits,
                key=lambda fit: (
                    (fit.region.board_center - board_point).norm,
                    fit.match.rms_distance_px,
                ),
            )
            return Shift.from_point(nearest.displacement)
        selected = min(
            covering,
            key=lambda fit: (
                sum((fit.displacement - other.displacement).norm for other in covering),
                fit.match.rms_distance_px,
                (fit.region.board_center - board_point).norm,
            ),
        )
        return Shift.from_point(selected.displacement)

    def summary(self) -> str:
        """照合の成功数（ジョブのログと結果に出す）."""
        refined = sum(r.result is not None for r in self.refinements)
        return (
            f"照合成功 {len(self.fits)}/{self.region_count} 領域 / "
            f"pad中心照合 {refined}/{len(self.refinements)} pads"
        )


@attrs.frozen
class AlignStep:
    """照合の 1 件に取りかかる（呼び出し側は進捗の表示と中断の確認に使う).

    Attributes:
        stage: 段階の名前（``"銅箔照合"`` / ``"pad照合"``）
        index: 段階の中で何件目か（0 始まり）
        total: 段階の件数
    """

    stage: str
    index: int
    total: int


@attrs.frozen
class RegionsPlanned:
    """照合する領域の数が決まった（照合を始める前）."""

    count: int

    @property
    def message(self) -> str:
        return f"照合対象の領域数: {self.count}"


@attrs.frozen
class RegionMeasured:
    """1 領域の照合を終えた（``fit`` が ``None`` なら失敗）."""

    region: AlignmentRegion
    fit: RegionFit | None

    @property
    def message(self) -> str:
        if self.fit is None:
            return f"警告: 領域 {self.region.index} の照合に失敗"
        displacement = self.fit.displacement
        return (
            f"領域 {self.region.index}: dx={displacement.x:+.4f} "
            f"dy={displacement.y:+.4f} mm, "
            f"rms_distance={self.fit.match.rms_distance_px:.2f} px, "
            f"passes={self.fit.passes}"
        )


@attrs.frozen
class RefinementPlanned:
    """Pad 中心の精密照合の対象が決まった（照合を始める前）.

    Attributes:
        target_count: 精密照合する pad の数
        pad_count: 位置合わせする pad の数
        max_short_side_mm: 対象を選んだ短辺の上限 [mm]
    """

    target_count: int
    pad_count: int
    max_short_side_mm: float

    @property
    def message(self) -> str:
        return (
            f"pad中心照合対象: {self.target_count}/{self.pad_count} pads "
            f"(最大短辺 {self.max_short_side_mm:g} mm)"
        )


@attrs.frozen
class PadMeasured:
    """1 pad の中心精密照合を終えた."""

    refinement: PadRefinement

    @property
    def message(self) -> str:
        pad = self.refinement.pad
        result = self.refinement.result
        residual = self.refinement.residual
        if result is None or residual is None:
            return (
                f"警告: {pad.designator}.{pad.pad_number} のpad中心照合が"
                "収束しないため領域補正を使用"
            )
        return (
            f"{pad.designator}.{pad.pad_number}: "
            f"residual=({residual.x:+.4f}, {residual.y:+.4f}) mm, "
            f"passes={result.passes}"
        )


type AlignEvent = (
    AlignStep | RegionsPlanned | RegionMeasured | RefinementPlanned | PadMeasured
)


def align_regions(
    session: RegionAlignmentSession,
    regions: Sequence[AlignmentRegion],
    *,
    on_event: Callable[[AlignEvent], None],
) -> BoardCorrection:
    """計画済みの領域を順に照合し、収束した領域から補正を作る.

    照合の失敗と非収束は、その領域の失敗として残りの領域へ進む。

    Raises:
        KlipperError: Klipper が移動を断った場合（残りの領域へ進まない）
        ValueError: 成功した領域が 1 件も無い場合
    """
    on_event(RegionsPlanned(len(regions)))
    fits: list[RegionFit] = []
    for index, region in enumerate(regions):
        on_event(AlignStep("銅箔照合", index, len(regions)))
        fit = session.align(region)
        on_event(RegionMeasured(region, fit))
        if fit is not None:
            fits.append(fit)
    return BoardCorrection(fits=tuple(fits), region_count=len(regions))


def refine_pad(
    session: RegionAlignmentSession, correction: BoardCorrection, pad: Pad
) -> PadRefinement:
    """領域補正を初期値に pad 中心で照合を収束させ、残差付きの結果を返す."""
    initial_correction = correction.correction_for(pad.center)
    refined = session.refine(pad.center, initial_correction, pad.polygon)
    if refined is None:
        return PadRefinement(pad=pad, result=None, residual=None)
    anchor = refined.region.anchor
    initial_displacement = initial_correction.apply(anchor) - anchor
    return PadRefinement(
        pad=pad, result=refined, residual=refined.displacement - initial_displacement
    )


def align_board(
    session: RegionAlignmentSession,
    pads: Sequence[Pad],
    *,
    max_short_side_mm: float,
    on_event: Callable[[AlignEvent], None],
) -> BoardCorrection:
    """Pads を覆う領域を照合し、短辺が ``max_short_side_mm`` 以下の pad を中心で精密照合する.

    精密照合の初期値は領域だけの補正で、収束しなかった pad は領域の補正を使う。

    Raises:
        KlipperError: Klipper が移動を断った場合
        ValueError: 成功した領域が 1 件も無い場合
    """
    regions = session.plan_regions([pad.center for pad in pads])
    correction = align_regions(session, regions, on_event=on_event)
    targets = refinement_targets(pads, max_short_side_mm=max_short_side_mm)
    on_event(RefinementPlanned(len(targets), len(pads), max_short_side_mm))
    refinements: list[PadRefinement] = []
    for index, pad in enumerate(targets):
        on_event(AlignStep("pad照合", index, len(targets)))
        refinement = refine_pad(session, correction, pad)
        on_event(PadMeasured(refinement))
        refinements.append(refinement)
    return attrs.evolve(correction, refinements=tuple(refinements))
