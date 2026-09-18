"""領域単位の反復銅箔位置合わせの仕様テスト."""

from typing import Any, override

import cv2
import numpy as np
import pytest
import shapely

from pcbasm import gcode as pcb_gcode
from pcbasm.geometry import Identity, Point2d, Point3d
from pcbasm.hal import Camera, Klipper, Speed, XYZStage
from pcbasm.posctrl import (
    AlignmentRegion,
    CopperEdgeMatcher,
    CopperProjector,
    RegionAligner,
)
from pcbasm.vision import CopperEdgeDetector, Image
from tests.helpers import FakeCamera

PPM = 100.0
IMAGE_SIZE = (200, 200)


def _image(shift_x: int = 0, shift_y: int = 0) -> Image:
    frame = np.zeros((IMAGE_SIZE[1], IMAGE_SIZE[0], 3), dtype=np.uint8)
    cv2.rectangle(
        frame,
        (60 + shift_x, 60 + shift_y),
        (140 + shift_x, 140 + shift_y),
        (255, 255, 255),
        thickness=-1,
    )
    return Image(frame)


class _FakeKlipper(Klipper):
    """送信 G-code だけを記録する pcbasm HAL fake."""

    def __init__(self) -> None:
        self.sent: list[pcb_gcode.GCode] = []

    @override
    def send_gcode(
        self, gcode: pcb_gcode.GCodeLike, *, timeout: float | None = None
    ) -> dict[str, Any]:
        self.sent.append(pcb_gcode.GCode(gcode))
        return {}


class _FakeStage(XYZStage):
    """指令位置を現在位置として公開する pcbasm HAL fake."""

    def __init__(self) -> None:
        self._position = Point3d(0.0, 0.0, 5.0)

    @override
    def move(
        self,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        *,
        speed: Speed | None = None,
        relative: bool = False,
    ) -> pcb_gcode.GCode:
        assert not relative
        self._position = Point3d(
            self._position.x if x is None else x,
            self._position.y if y is None else y,
            self._position.z if z is None else z,
        )
        return pcb_gcode.GCode("G1")

    @override
    def get_position(self) -> Point3d:
        return self._position


class _PositionRecordingCamera(FakeCamera):
    """Capture 時のステージ位置を記録する Camera HAL fake."""

    def __init__(self, images: list[Image], stage: XYZStage) -> None:
        super().__init__(images)
        self._stage = stage
        self._capture_positions: list[Point3d] = []

    @property
    def capture_positions(self) -> tuple[Point3d, ...]:
        return tuple(self._capture_positions)

    @override
    def capture(self) -> Image:
        self._capture_positions.append(self._stage.get_position())
        return super().capture()


def _region() -> AlignmentRegion:
    return AlignmentRegion(
        index=3,
        board_center=Point2d(0.0, 0.0),
        anchor=Point2d(0.0, 0.0),
        roi=(40, 40, 160, 160),
        board_area=shapely.box(-0.6, -0.6, 0.6, 0.6),
    )


def _aligner(
    shifts: list[tuple[int, int]],
    *,
    max_passes: int,
    converge_tolerance_mm: float,
    focus_z: float | None = None,
    stage: _FakeStage | None = None,
    camera: Camera | None = None,
) -> RegionAligner:
    stage = stage or _FakeStage()
    projector = CopperProjector(
        polygons=[shapely.box(-0.4, -0.4, 0.4, 0.4)],
        board_transform=Identity(),
        offset_transform=Identity(),
        pixel_per_mm=PPM,
        image_size=IMAGE_SIZE,
    )
    return RegionAligner(
        camera=camera or FakeCamera([_image(x, y) for x, y in shifts]),
        klipper=_FakeKlipper(),
        stage=stage,
        projector=projector,
        matcher=CopperEdgeMatcher(
            pixel_per_mm=PPM,
            search_window_mm=0.5,
        ),
        edge_detector=CopperEdgeDetector(),
        offset_transform=Identity(),
        focus_z=focus_z,
        max_correction_mm=1.0,
        max_passes=max_passes,
        converge_tolerance_mm=converge_tolerance_mm,
        settle_sec=0.0,
    )


class TestRegionAligner:
    """収束境界、累積変位、非収束棄却を公開 measure で検証する."""

    def test_increment_at_tolerance_converges(self):
        aligner = _aligner(
            [(3, 0)],
            max_passes=1,
            converge_tolerance_mm=0.03,
        )

        result = aligner.measure(_region())

        assert result.passes == 1
        assert result.increment.norm <= 0.03

    def test_accumulates_displacement_until_increment_converges(self):
        aligner = _aligner(
            [(15, 0), (5, 0)],
            max_passes=5,
            converge_tolerance_mm=0.06,
        )

        result = aligner.measure(_region())

        assert result.passes == 2
        assert result.displacement.x == pytest.approx(-0.2, abs=0.02)
        assert result.displacement.y == pytest.approx(0.0, abs=0.02)
        assert result.increment.norm <= 0.06

    def test_accumulates_from_initial_displacement(self):
        aligner = _aligner(
            [(5, 0)],
            max_passes=1,
            converge_tolerance_mm=0.06,
        )

        result = aligner.measure(_region(), initial_displacement=Point2d(0.2, -0.1))

        assert result.displacement.x == pytest.approx(0.15, abs=0.02)
        assert result.displacement.y == pytest.approx(-0.1, abs=0.02)

    def test_captures_every_pass_at_camera_focus_z(self):
        stage = _FakeStage()
        camera = _PositionRecordingCamera([_image(15, 0), _image(5, 0)], stage)
        aligner = _aligner(
            [(15, 0), (5, 0)],
            max_passes=5,
            converge_tolerance_mm=0.06,
            focus_z=-25.0,
            stage=stage,
            camera=camera,
        )

        result = aligner.measure(_region())

        assert result.passes == 2
        assert [position.z for position in camera.capture_positions] == pytest.approx(
            [-25.0, -25.0]
        )

    def test_rejects_region_that_does_not_converge_within_max_passes(self):
        aligner = _aligner(
            [(10, 0)] * 5,
            max_passes=5,
            converge_tolerance_mm=0.03,
        )

        with pytest.raises(RuntimeError, match="収束"):
            aligner.measure(_region())
