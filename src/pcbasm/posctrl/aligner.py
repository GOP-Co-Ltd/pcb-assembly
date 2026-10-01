"""領域単位の銅箔照合による位置ずれの反復計測."""

import attrs

from pcbasm.gcode import GCode
from pcbasm.geometry import Point2d, Shift, Transform
from pcbasm.hal import Camera, Klipper, Speed, XYZStage
from pcbasm.posctrl.copper import CopperEdgeMatcher, CopperProjector, EdgeMatch
from pcbasm.posctrl.correction import to_machine_transform
from pcbasm.posctrl.region import AlignmentRegion
from pcbasm.posctrl.render import render_edge_match
from pcbasm.vision import CopperEdgeDetector, FrameSink


@attrs.frozen
class RegionAlignment:
    """収束した 1 領域の照合結果.

    Attributes:
        region: 照合した領域
        match: 最終パスの照合結果
        displacement: 累積の機械変位（mm）。設計上の機械座標に足すと実位置になる
        increment: 最終パスの変位増分（mm）
        passes: 収束までのパス数
    """

    region: AlignmentRegion
    match: EdgeMatch
    displacement: Point2d
    increment: Point2d
    passes: int


class RegionAligner:
    """1 領域の機械変位を収束まで反復計測する."""

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
        focus_z: float | None = None,
        max_correction_mm: float = 1.0,
        max_passes: int = 5,
        converge_tolerance_mm: float = 0.03,
        settle_sec: float,
        frame_sink: FrameSink | None = None,
    ) -> None:
        """RegionAligner を初期化する."""
        if max_correction_mm <= 0:
            raise ValueError("max_correction_mmは正の値である必要があります")
        if max_passes < 1:
            raise ValueError("max_passesは1以上である必要があります")
        if converge_tolerance_mm <= 0:
            raise ValueError("converge_tolerance_mmは正の値である必要があります")
        self._camera = camera
        self._klipper = klipper
        self._stage = stage
        self._projector = projector
        self._matcher = matcher
        self._edge_detector = edge_detector
        self._offset_transform = offset_transform
        self._focus_z = focus_z
        self._max_correction_mm = max_correction_mm
        self._max_passes = max_passes
        self._converge_tolerance_mm = converge_tolerance_mm
        self._settle_sec = settle_sec
        self._frame_sink = frame_sink

    def measure(
        self,
        region: AlignmentRegion,
        *,
        initial_displacement: Point2d | None = None,
    ) -> RegionAlignment:
        """累積変位を投影へ反映し、収束した結果だけを返す.

        各パスでステージを ``region.anchor + 累積変位``（Z は focus_z）へ動かして撮像する。

        増分が converge_tolerance_mm 以下になったパスで返す。

        Args:
            region: 照合する領域
            initial_displacement: 累積変位の初期値（mm）。None なら 0

        Raises:
            RuntimeError: match 失敗、累積変位が max_correction_mm 超過、または max_passes で非収束
        """
        cumulative = initial_displacement or Point2d(0.0, 0.0)
        for passes in range(1, self._max_passes + 1):
            target = region.anchor + cumulative
            self._klipper.send_gcode(
                self._stage.move(
                    x=target.x,
                    y=target.y,
                    z=self._focus_z,
                    speed=Speed.rate(0.5),
                )
                + GCode.wait(self._settle_sec)
                + GCode.wait_for_done()
            )
            projector = self._projector.with_correction(Shift.from_point(cumulative))
            projection = projector.project(target)
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
                )

            machine_transform = to_machine_transform(
                match.camera_transform,
                self._offset_transform,
                projection_anchor=target,
                observed_at=self._stage.get_position().to2d(),
            )
            increment = machine_transform.apply(target) - target
            cumulative = cumulative + increment
            if cumulative.norm > self._max_correction_mm:
                raise RuntimeError(
                    f"照合ずれ {cumulative.norm:.3f} mm が上限 "
                    f"{self._max_correction_mm} mm を超過しました"
                )
            if increment.norm <= self._converge_tolerance_mm:
                return RegionAlignment(
                    region=region,
                    match=match,
                    displacement=cumulative,
                    increment=increment,
                    passes=passes,
                )

        raise RuntimeError(
            f"領域 {region.index} は {self._max_passes} パスで収束しませんでした"
        )
