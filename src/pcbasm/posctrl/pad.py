"""銅箔padの照合による自動位置合わせ."""

import logging
from collections.abc import Sequence

import attrs
from shapely import Polygon

from pcbasm import gcode
from pcbasm.geometry import Point2d, Rotation, Transform
from pcbasm.hal import Camera, Klipper, Speed, XYZStage
from pcbasm.pcb import Component, Pad
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
class ComponentPads:
    """部品とそのpaste pad群.

    部品の座標で計測した補正Transformを、部品に属する全padで共有する
    ための単位。

    Attributes:
        component: 部品
        pads: 部品に属するpad
    """

    component: Component
    pads: tuple[Pad, ...]


@attrs.frozen
class PadAlignmentTarget:
    """1個の実銅箔padを対象とする位置合わせ単位."""

    identifier: str
    pad: Pad

    @property
    def position(self) -> Point2d:
        """実銅箔ポリゴンの重心をboard座標で返す."""
        centroid = self.pad.copper_polygon.centroid
        return Point2d(x=float(centroid.x), y=float(centroid.y))


def group_pads_by_component(
    components: Sequence[Component], pads: Sequence[Pad]
) -> list[ComponentPads]:
    """padをdesignatorで部品に対応付けてグループ化する.

    Args:
        components: 対象部品列
        pads: 対象pad列

    Returns:
        padを1つ以上持つ部品のComponentPads（components順）
    """
    by_designator: dict[str, list[Pad]] = {}
    for pad in pads:
        by_designator.setdefault(pad.designator, []).append(pad)

    return [
        ComponentPads(component=c, pads=tuple(by_designator[c.designator]))
        for c in components
        if c.designator in by_designator
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
        anchor: 投影アンカー s0 = board_transform.apply(pad.center)（機械座標、mm）
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
    """銅箔照合による自動位置合わせを行うクラス.

    指令位置に固定したアンカーで想定銅箔を投影し、対象の実銅箔を覆う
    ROI限定の剛体照合とXYPositionAdjustorで収束させる。

    結果からmachine空間の補正Transformを構築する。
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
        roi_margin_mm: float = 1.0,
        min_roi_mm: float = 3.0,
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
            roi_margin_mm: pad投影bboxへ加えるROIマージン（mm）
            min_roi_mm: ROIの最小辺長（mm）
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
        self._roi_margin_mm = roi_margin_mm
        self._min_roi_mm = min_roi_mm
        self._tolerance = tolerance
        self._max_correction_mm = max_correction_mm
        self._max_iterations = max_iterations
        self._settle_time = settle_time
        self._frame_sink = frame_sink

        self._logger = logging.getLogger(get_class_module_path(self.__class__))

    def align(self, target: ComponentPads) -> PadAlignmentResult:
        """部品の座標へ移動し、銅箔照合で収束するまで位置補正する.

        ROIは部品に属する全padの実銅箔ポリゴンの投影bboxを覆うため、部品に
        含まれる銅箔の輪郭で照合される。投影アンカーは部品の指令位置
        s0 に固定し、収束ループ中は再投影しない（毎反復同位置で
        再投影すると補正が収束しない）。

        Args:
            target: 対象部品とそのpad群

        Returns:
            位置合わせ結果

        Raises:
            RuntimeError: 照合に失敗、または収束しなかった場合
        """
        return self._align(
            identifier=target.component.designator,
            position=target.component.position,
            copper_polygons=[pad.copper_polygon for pad in target.pads],
        )

    def align_pad(self, target: PadAlignmentTarget) -> PadAlignmentResult:
        """対象padの銅箔重心へ移動し、単一padのROIで位置合わせする.

        Args:
            target: 対象pad

        Returns:
            位置合わせ結果

        Raises:
            RuntimeError: 照合に失敗、または収束しなかった場合
        """
        return self._align(
            identifier=target.identifier,
            position=target.position,
            copper_polygons=[target.pad.copper_polygon],
        )

    def _align(
        self,
        *,
        identifier: str,
        position: Point2d,
        copper_polygons: Sequence[Polygon],
    ) -> PadAlignmentResult:
        """指定位置とROI用銅箔で位置合わせする共通処理."""
        anchor = self._board_transform.apply(position)
        self._logger.info(
            "%s の位置合わせを開始: anchor (%.4f, %.4f) mm",
            identifier,
            anchor.x,
            anchor.y,
        )

        # 部品の座標へ移動
        self._klipper.send_gcode(
            self._stage.move(x=anchor.x, y=anchor.y, speed=Speed.rate(0.5))
            + gcode.wait(self._settle_time)
            + gcode.wait_for_done()
        )

        # 投影とROIをアンカー s0 で固定する（ループ中は再投影しない）
        projection = self._projector.project(anchor)
        roi = self._projector.roi_of(
            copper_polygons,
            anchor,
            margin_mm=self._roi_margin_mm,
            min_size_mm=self._min_roi_mm,
        )

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
