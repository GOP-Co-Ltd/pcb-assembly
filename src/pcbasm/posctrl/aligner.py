"""領域単位の銅箔照合による位置ずれの反復計測."""

import attrs
from shapely import Polygon

from pcbasm import gcode
from pcbasm.geometry import Point2d, Shift, Transform
from pcbasm.hal import Camera, Klipper, Speed, XYZStage
from pcbasm.posctrl.copper import CopperEdgeMatcher, CopperProjector, EdgeMatch
from pcbasm.posctrl.correction import to_machine_transform
from pcbasm.posctrl.region import AlignmentRegion
from pcbasm.posctrl.render import render_edge_match
from pcbasm.vision import CopperEdgeDetector, FrameSink


@attrs.frozen
class RegionAlignment:
    """収束した1領域の照合結果."""

    region: AlignmentRegion
    match: EdgeMatch
    displacement: Point2d
    increment: Point2d
    passes: int


class RegionAligner:
    """1領域の機械変位を収束まで反復計測する."""

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
        match_area: Polygon,
        max_correction_mm: float = 1.0,
        max_passes: int = 5,
        converge_tolerance_mm: float = 0.03,
        settle_time: float = 0.5,
        frame_sink: FrameSink | None = None,
    ) -> None:
        """RegionAlignerを初期化する."""
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
        self._match_area = match_area
        self._max_correction_mm = max_correction_mm
        self._max_passes = max_passes
        self._converge_tolerance_mm = converge_tolerance_mm
        self._settle_time = settle_time
        self._frame_sink = frame_sink

    def measure(self, region: AlignmentRegion) -> RegionAlignment:
        """累積変位を投影へ反映し、収束した結果だけを返す.

        Raises:
            RuntimeError: match失敗、最大補正超過、または最大パス数で非収束
        """
        cumulative = Point2d(0.0, 0.0)
        for passes in range(1, self._max_passes + 1):
            target = region.anchor + cumulative
            self._klipper.send_gcode(
                self._stage.move(x=target.x, y=target.y, speed=Speed.rate(0.5))
                + gcode.wait(self._settle_time)
                + gcode.wait_for_done()
            )
            projector = self._projector.with_correction(Shift.from_point(cumulative))
            projection = projector.project(target)
            image = self._camera.capture()
            edges = self._edge_detector.detect_edges(image)
            if self._frame_sink is not None:
                self._frame_sink(
                    render_edge_match(image, edges, projection.edge_mask, region.roi)
                )
            match = self._matcher.match(
                edges,
                projection.edge_mask,
                region.roi,
                mask=projector.project_mask(self._match_area, target),
            )
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
