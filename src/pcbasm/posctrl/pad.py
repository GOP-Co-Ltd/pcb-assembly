"""関心領域(ROI)単位の銅箔照合による自動位置合わせ."""

import logging
import math
from collections.abc import Sequence

import attrs
import shapely
from shapely import Polygon

from pcbasm import gcode
from pcbasm.geometry import Point2d, Rotation, Transform
from pcbasm.hal import Camera, Klipper, Speed, XYZStage
from pcbasm.pcb import Pad
from pcbasm.posctrl.copper import (
    CopperEdgeMatcher,
    CopperProjection,
    CopperProjector,
    PixelRect,
    RigidEdgeMatch,
)
from pcbasm.posctrl.correction import to_machine_transform
from pcbasm.posctrl.position import XYPositionAdjustor
from pcbasm.posctrl.render import render_edge_match
from pcbasm.utils import get_class_module_path
from pcbasm.vision import CopperEdgeDetector, FrameSink


@attrs.frozen
class PadRegion:
    """Board原点固定グリッドで分割した pad の関心領域(ROI)単位.

    Attributes:
        key: グリッドインデックス (col, row)。board原点(0,0)固定
        bounds: セル矩形 (minx, miny, maxx, maxy) [mm]
        pads: 割り当てられたpad（常に1つ以上）
    """

    key: tuple[int, int]
    bounds: tuple[float, float, float, float]
    pads: tuple[Pad, ...]

    @property
    def center(self) -> Point2d:
        """セル矩形中心（board座標、mm）。照合アンカー."""
        minx, miny, maxx, maxy = self.bounds
        return Point2d(x=(minx + maxx) / 2, y=(miny + maxy) / 2)

    @property
    def box(self) -> Polygon:
        """セル矩形のshapely Polygon（roi_of・renderer用）."""
        return shapely.box(*self.bounds)

    @property
    def label(self) -> str:
        """グリッドインデックスに基づくラベル（例 "C3R5"）."""
        col, row = self.key
        return f"C{col}R{row}"

    @property
    def designators(self) -> tuple[str, ...]:
        """領域内padの重複除去・ソート済みdesignator（表示用）."""
        return tuple(sorted({pad.designator for pad in self.pads}))


def _cell_bounds(
    key: tuple[int, int], width: float, height: float
) -> tuple[float, float, float, float]:
    """グリッドインデックスからセル矩形bounds (minx, miny, maxx, maxy) を返す."""
    col, row = key
    return (col * width, row * height, (col + 1) * width, (row + 1) * height)


def _cell_box(key: tuple[int, int], width: float, height: float) -> Polygon:
    """グリッドインデックスからセル矩形のshapely Polygonを返す."""
    return shapely.box(*_cell_bounds(key, width, height))


def _assign_cell(pad: Pad, width: float, height: float) -> tuple[int, int]:
    """padを割り当てるグリッドセルのインデックスを決める.

    pad.centerのfloor除算で決まる中心セルへ割り当てる。ただし実銅箔
    (copper_polygon)の輪郭が中心セルと交差しない巨大pad（サーマルパッド等、
    セルが銅箔内部に完全に沈むケース）は、輪郭とセル矩形の交差長が最大の セルへ再割当てする（エッジ皆無セルでの照合失敗を回避）。
    """
    center = pad.center
    primary = (math.floor(center.x / width), math.floor(center.y / height))
    exterior = pad.copper_polygon.exterior
    if exterior.intersects(_cell_box(primary, width, height)):
        return primary
    return _reassign_by_exterior_overlap(exterior, primary, width, height)


def _reassign_by_exterior_overlap(
    exterior: shapely.LinearRing,
    primary: tuple[int, int],
    width: float,
    height: float,
) -> tuple[int, int]:
    """輪郭とセル矩形の交差長が最大となるセルへ再割当てする.

    候補は輪郭のbboxが重なるセル全体。タイブレークは(col, row)昇順
    （列優先の昇順走査で最初に見つかった最大値を保持することで実現する）。
    """
    minx, miny, maxx, maxy = exterior.bounds
    col_lo, col_hi = math.floor(minx / width), math.floor(maxx / width)
    row_lo, row_hi = math.floor(miny / height), math.floor(maxy / height)

    best_key = primary
    best_length = -1.0
    for col in range(col_lo, col_hi + 1):
        for row in range(row_lo, row_hi + 1):
            key = (col, row)
            length = exterior.intersection(_cell_box(key, width, height)).length
            if length > best_length:
                best_length = length
                best_key = key
    return best_key


def plan_pad_regions(
    pads: Sequence[Pad], region_size: tuple[float, float]
) -> list[PadRegion]:
    """padをboard原点固定グリッドの関心領域(ROI)単位に分割する.

    セル = region_size (w, h) [mm] のグリッド（board原点(0,0)固定）へ、
    pad.centerのfloor除算で割り当てる。実銅箔の輪郭が中心セルと交差しない
    巨大pad（サーマルパッド等）は境界セルへ再割当てされる
    （``_assign_cell`` 参照）。

    Args:
        pads: 対象pad列
        region_size: セルサイズ (width, height) [mm]

    Returns:
        padを1つ以上持つ領域を(col, row)昇順で返す。空入力は[]
    """
    width, height = region_size
    assignments: dict[tuple[int, int], list[Pad]] = {}
    for pad in pads:
        key = _assign_cell(pad, width, height)
        assignments.setdefault(key, []).append(pad)

    return [
        PadRegion(
            key=key,
            bounds=_cell_bounds(key, width, height),
            pads=tuple(assignments[key]),
        )
        for key in sorted(assignments)
    ]


class CopperPadObserver:
    """固定アンカー投影に対するpad ROI照合のobserver.

    observe() -> Transform 契約（カメラmm空間、原点=画像中心、想定→観測）。
    """

    def __init__(
        self,
        camera: Camera,
        edge_detector: CopperEdgeDetector,
        matcher: CopperEdgeMatcher,
        projection: CopperProjection,
        roi: PixelRect,
        frame_sink: FrameSink | None = None,
        max_offset_mm: float | None = None,
    ) -> None:
        """CopperPadObserverを初期化する.

        Args:
            camera: カメラ
            edge_detector: 銅箔エッジ検出器
            matcher: エッジ照合器
            projection: アンカー位置で固定した想定銅箔の投影
            roi: 照合に使うROI矩形（全画面px）
            frame_sink: 観測ごとに照合状況フレームを送る sink。
                Noneの場合は送らない
            max_offset_mm: 照合ずれの許容上限（mm）。boardキャリブレーション
                済みでpadはほぼ合っている前提のもと、これを超えるずれは
                誤マッチとみなして照合失敗にする。Noneの場合は無制限
        """
        self._camera = camera
        self._edge_detector = edge_detector
        self._matcher = matcher
        self._projection = projection
        self._roi = roi
        self._frame_sink = frame_sink
        self._max_offset_mm = max_offset_mm
        self._last_match: RigidEdgeMatch | None = None

    def observe(self) -> Transform:
        """撮像→エッジ検出→剛体照合し、想定→観測のTransformを返す.

        Returns:
            想定→観測のTransform（カメラmm空間）

        Raises:
            RuntimeError: 照合に失敗した場合、または照合ずれが
                max_offset_mmを超えた場合
        """
        image = self._camera.capture()
        edges = self._edge_detector.detect_edges(image)
        if self._frame_sink is not None:
            self._frame_sink(
                render_edge_match(image, edges, self._projection.edge_mask, self._roi)
            )
        match = self._matcher.match_rigid(
            edges, self._projection.edge_mask, roi=self._roi
        )
        if match is None:
            raise RuntimeError("銅箔エッジの照合に失敗しました")
        offset_norm = match.offset.mm.norm
        if self._max_offset_mm is not None and offset_norm > self._max_offset_mm:
            raise RuntimeError(
                f"照合ずれ {offset_norm:.3f} mm が上限 "
                f"{self._max_offset_mm} mm を超過しました（誤マッチの疑い）"
            )
        self._last_match = match
        return match.camera_transform

    @property
    def last_match(self) -> RigidEdgeMatch | None:
        """直近の照合結果."""
        return self._last_match


@attrs.frozen
class PadAlignmentResult:
    """pad位置合わせの結果.

    Attributes:
        machine_transform: 設計machine点→観測machine点の変換（fill path合成用）
        match: 最終照合結果（表示用）
        anchor: 投影アンカー s0 = board_transform.apply(region.center)（機械座標、mm）
        adjusted_position: 収束後のXY位置（機械座標、mm）
        roi: 照合に使ったROI矩形（全画面px）
    """

    machine_transform: Transform
    match: RigidEdgeMatch
    anchor: Point2d
    adjusted_position: Point2d
    roi: PixelRect

    @property
    def translation(self) -> Point2d:
        """アンカー点での並進補正量（machine mm）."""
        return self.machine_transform.apply(self.anchor) - self.anchor

    @property
    def rotation(self) -> Rotation:
        """machine空間での回転成分（鏡映も自動処理）."""
        ex = Point2d(1.0, 0.0)
        origin = self.machine_transform.apply(self.anchor)
        moved = self.machine_transform.apply(self.anchor + ex)
        return Rotation.from_points(ex, moved - origin)


class PadAligner:
    """関心領域(ROI)単位で銅箔照合による自動位置合わせを行うクラス.

    領域の座標へ移動し、指令位置に固定したアンカーで想定銅箔を投影、
    領域矩形を覆うROI限定の剛体照合とXYPositionAdjustorで収束させ、
    machine空間の補正Transformを構築する。
    """

    def __init__(
        self,
        *,
        camera: Camera,
        klipper: Klipper,
        stage: XYZStage,
        projector: CopperProjector,
        matcher: CopperEdgeMatcher,
        edge_detector: CopperEdgeDetector,
        board_transform: Transform,
        offset_transform: Transform,
        image_size: tuple[int, int],
        search_window_px: int,
        roi_margin_mm: float = 1.0,
        tolerance: float = 0.05,
        max_correction_mm: float | None = 1.0,
        max_iterations: int = 10,
        settle_time: float = 0.5,
        frame_sink: FrameSink | None = None,
    ) -> None:
        """PadAlignerを初期化する.

        Args:
            camera: カメラ
            klipper: Klipperクライアント
            stage: XYZステージ
            projector: 設計銅箔の投影器
            matcher: エッジ照合器
            edge_detector: 銅箔エッジ検出器
            board_transform: board座標→機械座標の変換
            offset_transform: 観測オフセット系から機械座標系への変換
            image_size: 撮像フレームサイズ (width, height) [px]
            search_window_px: 照合の探索窓 片側幅 [px]。ROIがこの分の
                余白をフレーム内に確保できるかの検証に使う
            roi_margin_mm: 領域矩形へ加えるROIマージン（mm）
            tolerance: 収束の許容誤差（mm）
            max_correction_mm: 1回の照合で許容する最大ずれ（mm）。
                超過は誤マッチとみなして照合失敗にする。Noneは無制限
            max_iterations: 収束ループの最大反復回数
            settle_time: 移動後の安定待機時間（秒）
            frame_sink: 観測ごとに照合状況フレームを送る sink。
                Noneの場合は送らない
        """
        self._camera = camera
        self._klipper = klipper
        self._stage = stage
        self._projector = projector
        self._matcher = matcher
        self._edge_detector = edge_detector
        self._board_transform = board_transform
        self._offset_transform = offset_transform
        self._image_size = image_size
        self._search_window_px = search_window_px
        self._roi_margin_mm = roi_margin_mm
        self._tolerance = tolerance
        self._max_correction_mm = max_correction_mm
        self._max_iterations = max_iterations
        self._settle_time = settle_time
        self._frame_sink = frame_sink

        self._logger = logging.getLogger(get_class_module_path(self.__class__))

    def align(self, target: PadRegion) -> PadAlignmentResult:
        """領域の座標へ移動し、銅箔照合で収束するまで位置補正する.

        ROIは領域矩形（target.box）の投影bboxを覆う。投影アンカーは領域
        矩形中心 s0 = board_transform.apply(target.center) に固定し、
        収束ループ中は再投影しない（毎反復同位置で再投影すると補正が
        収束しない）。

        Args:
            target: 対象領域

        Returns:
            位置合わせ結果

        Raises:
            RuntimeError: 照合に失敗、または収束しなかった場合
            ValueError: ROIがフレーム（search_window inset）に収まらない場合
        """
        anchor = self._board_transform.apply(target.center)
        self._logger.info(
            "領域 %s の位置合わせを開始: anchor (%.4f, %.4f) mm",
            target.label,
            anchor.x,
            anchor.y,
        )

        # 領域の座標へ移動
        self._klipper.send_gcode(
            self._stage.move(x=anchor.x, y=anchor.y, speed=Speed.rate(0.5))
            + gcode.wait(self._settle_time)
            + gcode.wait_for_done()
        )

        # 投影とROIをアンカー s0 で固定する（ループ中は再投影しない）
        projection = self._projector.project(anchor)
        roi = self._projector.roi_of(
            [target.box], anchor, margin_mm=self._roi_margin_mm
        )
        self._validate_roi_fits_frame(roi, target)

        observer = CopperPadObserver(
            camera=self._camera,
            edge_detector=self._edge_detector,
            matcher=self._matcher,
            projection=projection,
            roi=roi,
            frame_sink=self._frame_sink,
            max_offset_mm=self._max_correction_mm,
        )
        adjustor = XYPositionAdjustor(
            observe=observer.observe,
            klipper=self._klipper,
            stage=self._stage,
            offset_transform=self._offset_transform,
            tolerance=self._tolerance,
            max_iterations=self._max_iterations,
            settle_time=self._settle_time,
        )
        adjusted_position = adjustor.adjust()

        # adjust() が成功した時点で observe() は少なくとも1回成功している
        match = observer.last_match
        assert match is not None

        machine_transform = to_machine_transform(
            match.camera_transform,
            self._offset_transform,
            projection_anchor=anchor,
            observed_at=self._stage.get_position().to2d(),
        )
        return PadAlignmentResult(
            machine_transform=machine_transform,
            match=match,
            anchor=anchor,
            adjusted_position=adjusted_position,
            roi=roi,
        )

    def _validate_roi_fits_frame(self, roi: PixelRect, target: PadRegion) -> None:
        """ROIがフレームをsearch_window分の余白込みで収まっているか検証する.

        収まらない場合、従来はroi_of内部でフレーム端に静かにクランプされ
        精度劣化が可視化されなかった。これを排除するためValueErrorにする。

        Raises:
            ValueError: ROIがフレーム（search_window inset）に収まらない場合
        """
        width, height = self._image_size
        window = self._search_window_px
        x0, y0, x1, y1 = roi
        if x0 < window or y0 < window or x1 > width - window or y1 > height - window:
            raise ValueError(
                f"領域 {target.label} のROI {roi} が視野 {width}x{height}px に"
                f"search_window={window}px の余白込みで収まりません。"
                "camera.crop を縮小するか、pad_align の"
                "roi_margin/search_window を調整してください"
            )
