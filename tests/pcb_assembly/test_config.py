from pathlib import Path

import pytest

from pcb_assembly.config import (
    Camera,
    CameraCrop,
    Corner,
    Klipper,
    Machine,
    PasteDispenser,
    Probe,
    ReferencePoint,
    Toolhead,
)
from pcb_assembly.geometry import Point2d
from tests.helpers import TESTING_DATA_DIR


class TestMachine:
    """Machineクラスのテスト."""

    def test_load_config(self):
        machine = Machine(TESTING_DATA_DIR / "machine.toml")

        assert machine.klipper == Klipper(host="192.168.1.100", port=7125)
        assert machine.probe == Probe(
            a_pin=17,
            b_pin=27,
            rotation_pulse=600,
            rotation_distance=40.0,
            inverse=True,
        )
        assert machine.paste_dispenser == PasteDispenser(
            syringe_size=9.0,
            nozzle_size="G27",
        )
        assert machine.camera == Camera(
            device_id=0,
            width=640,
            height=480,
            fps=30.0,
            format="YUYV",
            crop=CameraCrop(width=400, height=400),
            calibration_file=TESTING_DATA_DIR / "calibration.json",
        )
        assert machine.toolhead == Toolhead(x=13.2, y=54.7)
        assert machine.reference_point == ReferencePoint(
            x=23.1,
            y=8.3,
            offset_x=0,
            offset_y=5,
            target_diameter=3.0,
        )

    def test_default_values(self):
        machine = Machine(TESTING_DATA_DIR / "machine_minimal.toml")

        assert machine.klipper == Klipper(host="localhost", port=7125)
        assert machine.probe.inverse is False
        assert machine.camera.device_id == 0
        assert machine.camera.format == "YUYV"

    def test_raises_key_error_when_config_not_defined(self):
        machine = Machine(TESTING_DATA_DIR / "machine_minimal.toml")

        with pytest.raises(
            KeyError, match="'paste_dispenser' は設定ファイルに定義されていません"
        ):
            machine.paste_dispenser


class TestCamera:
    """Cameraクラスのテスト."""

    def test_size(self):
        camera = Camera(
            width=640,
            height=480,
            fps=30.0,
            crop=CameraCrop(width=400, height=400),
            calibration_file=Path("calibration.json"),
        )

        assert camera.size == (640, 480)


class TestCameraCrop:
    """CameraCropクラスのテスト."""

    def test_size(self):
        crop = CameraCrop(width=400, height=300)

        assert crop.size == (400, 300)


class TestReferencePoint:
    """ReferencePointクラスのテスト."""

    def test_to_point(self):
        ref = ReferencePoint(
            x=10.0, y=20.0, offset_x=1.0, offset_y=2.0, target_diameter=3.0
        )

        assert ref.to_point() == Point2d(10.0, 20.0)

    def test_get_reference_position_default_is_top_left(self):
        ref = ReferencePoint(
            x=10.0, y=20.0, offset_x=1.0, offset_y=2.0, target_diameter=3.0
        )

        assert ref.get_reference_position() == Point2d(10.0, 20.0)

    def test_get_reference_position_top_right(self):
        ref = ReferencePoint(
            x=10.0, y=20.0, offset_x=1.0, offset_y=2.0, target_diameter=3.0
        )

        # top_right = x + 2*offset_x + board_width = 10 + 2*1 + 100 = 112
        assert ref.get_reference_position(
            Corner.TOP_RIGHT, board_width=100.0
        ) == Point2d(112.0, 20.0)

    def test_get_reference_position_top_right_requires_board_width(self):
        ref = ReferencePoint(
            x=10.0, y=20.0, offset_x=1.0, offset_y=2.0, target_diameter=3.0
        )

        with pytest.raises(ValueError, match="board_widthが必要"):
            ref.get_reference_position(Corner.TOP_RIGHT)

    def test_offset_from_board_default_is_top_left(self):
        ref = ReferencePoint(
            x=10.0, y=20.0, offset_x=1.0, offset_y=2.0, target_diameter=3.0
        )

        assert ref.offset_from_board() == Point2d(-1.0, -2.0)

    def test_offset_from_board_top_right(self):
        ref = ReferencePoint(
            x=10.0, y=20.0, offset_x=1.0, offset_y=2.0, target_diameter=3.0
        )

        assert ref.offset_from_board(Corner.TOP_RIGHT) == Point2d(1.0, -2.0)
