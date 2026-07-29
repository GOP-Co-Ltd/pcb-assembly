"""領域単位の銅箔照合による位置ずれの1ショット計測."""

import logging

import attrs

from pcbasm import gcode
from pcbasm.geometry import Point2d, Transform
from pcbasm.hal import Camera, Klipper, Speed, XYZStage
from pcbasm.posctrl.copper import CopperEdgeMatcher, CopperProjector, EdgeMatch
from pcbasm.posctrl.correction import to_machine_transform
from pcbasm.posctrl.region import AlignmentRegion
from pcbasm.posctrl.render import render_edge_match
from pcbasm.utils import get_class_module_path
from pcbasm.vision import CopperEdgeDetector, FrameSink


@attrs.frozen
class RegionAlignment:
    """1領域の照合結果.

    照合が並進のみなので machine_transform も純並進になり、補正量はアンカーからの
    距離に依存しない。

    Attributes:
        region: 対象領域
        match: 照合結果
        machine_transform: 設計machine点→観測machine点の変換（純並進）
    """

    region: AlignmentRegion
    match: EdgeMatch
    machine_transform: Transform

    @property
    def translation(self) -> Point2d:
        """Machine空間の並進補正量 [mm]."""
        anchor = self.region.anchor
        return self.machine_transform.apply(anchor) - anchor


class RegionAligner:
    """領域単位で銅箔照合による位置ずれを1ショット計測するクラス.

    領域のアンカーへ移動し、その指令位置に固定したアンカーで想定銅箔を投影、
    領域ROI限定の並進照合を1回だけ行ってmachine空間の補正Transformを構築する。
    収束ループは持たない（補正の適用は機械座標へ出る瞬間に1回だけ行う）。
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
        offset_transform: Transform,
        max_correction_mm: float | None = 1.0,
        settle_time: float = 0.5,
        frame_sink: FrameSink | None = None,
    ) -> None:
        """RegionAlignerを初期化する.

        Args:
            camera: カメラ
            klipper: Klipperクライアント
            stage: XYZステージ
            projector: 設計銅箔の投影器
            matcher: エッジ照合器
            edge_detector: 銅箔エッジ検出器
            offset_transform: 観測オフセット系から機械座標系への変換
            max_correction_mm: 1回の照合で許容する最大ずれ（mm）。
                超過は誤マッチとみなして照合失敗にする。Noneは無制限
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
        self._offset_transform = offset_transform
        self._max_correction_mm = max_correction_mm
        self._settle_time = settle_time
        self._frame_sink = frame_sink

        self._logger = logging.getLogger(get_class_module_path(self.__class__))

    def measure(self, region: AlignmentRegion) -> RegionAlignment:
        """領域のアンカーへ移動し、1回の撮像で並進ずれを計測する.

        Args:
            region: 対象領域

        Returns:
            領域の照合結果

        Raises:
            RuntimeError: 照合に失敗した場合、または照合ずれが
                max_correction_mmを超えた場合
        """
        anchor = region.anchor
        self._logger.info(
            "領域 %d の照合を開始: anchor (%.4f, %.4f) mm",
            region.index,
            anchor.x,
            anchor.y,
        )
        self._klipper.send_gcode(
            self._stage.move(x=anchor.x, y=anchor.y, speed=Speed.rate(0.5))
            + gcode.wait(self._settle_time)
            + gcode.wait_for_done()
        )

        projection = self._projector.project(anchor)
        image = self._camera.capture()
        edges = self._edge_detector.detect_edges(image)
        if self._frame_sink is not None:
            self._frame_sink(
                render_edge_match(image, edges, projection.edge_mask, region.roi)
            )

        match = self._matcher.match(edges, projection.edge_mask, region.roi)
        if match is None:
            raise RuntimeError(
                f"領域 {region.index} の銅箔エッジ照合に失敗しました"
                "（拘束不足または観測エッジなし）"
            )
        offset_norm = match.offset.mm.norm
        if (
            self._max_correction_mm is not None
            and offset_norm > self._max_correction_mm
        ):
            raise RuntimeError(
                f"照合ずれ {offset_norm:.3f} mm が上限 "
                f"{self._max_correction_mm} mm を超過しました（誤マッチの疑い）"
            )

        machine_transform = to_machine_transform(
            match.camera_transform,
            self._offset_transform,
            projection_anchor=anchor,
            observed_at=self._stage.get_position().to2d(),
        )
        return RegionAlignment(
            region=region, match=match, machine_transform=machine_transform
        )
