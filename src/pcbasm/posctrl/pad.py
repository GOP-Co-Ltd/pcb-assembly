"""pad単位の銅箔照合による自動位置合わせ."""

import logging

import attrs

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
from pcbasm.utils import get_class_module_path
from pcbasm.vision import CopperEdgeDetector


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
    ) -> None:
        """CopperPadObserverを初期化する.

        Args:
            camera: カメラ
            edge_detector: 銅箔エッジ検出器
            matcher: エッジ照合器
            projection: アンカー位置で固定した想定銅箔の投影
            roi: 照合に使うROI矩形（全画面px）
        """
        self._camera = camera
        self._edge_detector = edge_detector
        self._matcher = matcher
        self._projection = projection
        self._roi = roi
        self._last_match: RigidEdgeMatch | None = None

    def observe(self) -> Transform:
        """撮像→エッジ検出→剛体照合し、想定→観測のTransformを返す.

        Returns:
            想定→観測のTransform（カメラmm空間）

        Raises:
            RuntimeError: 照合に失敗した場合
        """
        edges = self._edge_detector.detect_edges(self._camera.capture())
        match = self._matcher.match_rigid(
            edges, self._projection.edge_mask, roi=self._roi
        )
        if match is None:
            raise RuntimeError("銅箔エッジの照合に失敗しました")
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
    """pad単位で銅箔照合による自動位置合わせを行うクラス.

    pad中心へ移動し、指令位置に固定したアンカーで想定銅箔を投影、 pad
    ROI限定の剛体照合とXYPositionAdjustorで収束させ、 machine空間の補正Transformを構築する。
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
        max_iterations: int = 10,
        settle_time: float = 0.5,
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
            max_iterations: 収束ループの最大反復回数
            settle_time: 移動後の安定待機時間（秒）
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
        self._max_iterations = max_iterations
        self._settle_time = settle_time

        self._logger = logging.getLogger(get_class_module_path(self.__class__))

    def align(self, pad: Pad) -> PadAlignmentResult:
        """padの中心へ移動し、銅箔照合で収束するまで位置補正する.

        投影アンカーはpadの指令位置 s0 に固定し、収束ループ中は
        再投影しない（毎反復同位置で再投影すると補正が収束しない）。

        Args:
            pad: 対象pad

        Returns:
            位置合わせ結果

        Raises:
            RuntimeError: 照合に失敗、または収束しなかった場合
        """
        anchor = self._board_transform.apply(pad.center)
        self._logger.info(
            "pad %s.%s の位置合わせを開始: anchor (%.4f, %.4f) mm",
            pad.designator,
            pad.pad_number,
            anchor.x,
            anchor.y,
        )

        # pad中心へ移動
        self._klipper.send_gcode(
            self._stage.move(x=anchor.x, y=anchor.y, speed=Speed.rate(0.5))
            + gcode.wait(self._settle_time)
            + gcode.wait_for_done()
        )

        # 投影とROIをアンカー s0 で固定する（ループ中は再投影しない）
        projection = self._projector.project(anchor)
        roi = self._projector.roi_of(
            pad.polygon,
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
