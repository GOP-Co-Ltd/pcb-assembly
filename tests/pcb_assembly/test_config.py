from pathlib import Path

import pytest

from pcb_assembly.config import (
    Camera,
    CameraCrop,
    Corner,
    CornerOffsets,
    Klipper,
    Machine,
    PasteDispenser,
    ReferencePoint,
    Toolhead,
    get_machine_config,
)
from pcb_assembly.geometry import Point2d, Shift
from tests.helpers import TESTING_DATA_DIR


class TestMachine:
    """Machineクラスのテスト."""

    def test_load_config(self):
        machine = Machine(TESTING_DATA_DIR / "machine.toml")

        assert machine.klipper == Klipper(host="192.168.1.100", port=7125)
        assert machine.paste_dispenser == PasteDispenser(
            rotations_per_ul=1.0,
            nozzle_size="27G",
            dispense_rate=5.0,
            dispense_accel=10.0,
            retract_amount=10.0,
            retract_rate=50.0,
            retract_accel_factor=2.0,
            toolhead=Toolhead(x=13.2, y=54.7),
            paste_velocity=5.0,
            paste_height=0.1,
            paste_thickness=0.1,
            ul_per_mm2=0.2,
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
        assert machine.reference_point == ReferencePoint(
            x=23.1,
            y=8.3,
            target_diameter=3.0,
            offsets=CornerOffsets(
                top_left=(0.0, -5.0),
                top_right=(0.0, -5.0),
                bottom_left=(5.0, 5.0),
            ),
        )

    def test_default_values(self):
        machine = Machine(TESTING_DATA_DIR / "machine_minimal.toml")

        assert machine.klipper == Klipper(host="localhost", port=7125)
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


class TestToolhead:
    """Toolheadクラスのテスト."""

    def test_to_transform(self):
        toolhead = Toolhead(x=13.2, y=54.7)

        assert toolhead.to_transform() == Shift(x=13.2, y=54.7)


class TestCornerOffsets:
    """CornerOffsetsクラスのテスト."""

    def test_requires_at_least_two_non_top_left_corners(self):
        with pytest.raises(ValueError, match="少なくとも2つ"):
            CornerOffsets(top_left=(0.0, -5.0))

    def test_requires_at_least_two_non_top_left_corners_with_one(self):
        with pytest.raises(ValueError, match="少なくとも2つ"):
            CornerOffsets(top_left=(0.0, -5.0), top_right=(0.0, -5.0))

    def test_has_corner_returns_true_for_defined_corners(self):
        offsets = CornerOffsets(
            top_left=(0.0, -5.0),
            top_right=(0.0, -5.0),
            bottom_left=(5.0, 5.0),
        )

        assert offsets.has_corner(Corner.TOP_LEFT) is True
        assert offsets.has_corner(Corner.TOP_RIGHT) is True
        assert offsets.has_corner(Corner.BOTTOM_LEFT) is True
        assert offsets.has_corner(Corner.BOTTOM_RIGHT) is False

    def test_get_returns_point2d(self):
        offsets = CornerOffsets(
            top_left=(1.0, -2.0),
            top_right=(3.0, -4.0),
            bottom_left=(5.0, 5.0),
        )

        assert offsets.get(Corner.TOP_LEFT) == Point2d(1.0, -2.0)
        assert offsets.get(Corner.TOP_RIGHT) == Point2d(3.0, -4.0)

    def test_get_raises_for_undefined_corner(self):
        offsets = CornerOffsets(
            top_left=(0.0, -5.0),
            top_right=(0.0, -5.0),
            bottom_left=(5.0, 5.0),
        )

        with pytest.raises(ValueError, match="BOTTOM_RIGHT"):
            offsets.get(Corner.BOTTOM_RIGHT)


class TestReferencePoint:
    """ReferencePointクラスのテスト."""

    def test_to_point(self):
        ref = ReferencePoint(
            x=10.0,
            y=20.0,
            target_diameter=3.0,
            offsets=CornerOffsets(
                top_left=(1.0, -2.0),
                top_right=(1.0, -2.0),
                bottom_left=(5.0, 5.0),
            ),
        )

        assert ref.to_point() == Point2d(10.0, 20.0)

    def test_get_reference_position_default_is_top_left(self):
        ref = ReferencePoint(
            x=10.0,
            y=20.0,
            target_diameter=3.0,
            offsets=CornerOffsets(
                top_left=(1.0, -2.0),
                top_right=(1.0, -2.0),
                bottom_left=(5.0, 5.0),
            ),
        )

        assert ref.get_reference_position() == Point2d(10.0, 20.0)

    def test_get_reference_position_top_right(self):
        # ref = (10, 20), offset_top_left = (1, -2)
        # board_origin = (10, 20) - (1, -2) = (9, 22)
        # board_top_right = (9 + 100, 22) = (109, 22)
        # ref_top_right = (109, 22) + (3, -4) = (112, 18)
        ref = ReferencePoint(
            x=10.0,
            y=20.0,
            target_diameter=3.0,
            offsets=CornerOffsets(
                top_left=(1.0, -2.0),
                top_right=(3.0, -4.0),
                bottom_left=(5.0, 5.0),
            ),
        )

        assert ref.get_reference_position(
            Corner.TOP_RIGHT, board_width=100.0
        ) == Point2d(112.0, 18.0)

    def test_get_reference_position_top_right_requires_board_width(self):
        ref = ReferencePoint(
            x=10.0,
            y=20.0,
            target_diameter=3.0,
            offsets=CornerOffsets(
                top_left=(1.0, -2.0),
                top_right=(1.0, -2.0),
                bottom_left=(5.0, 5.0),
            ),
        )

        with pytest.raises(ValueError, match="board_widthが必要"):
            ref.get_reference_position(Corner.TOP_RIGHT)

    def test_get_reference_position_bottom_left(self):
        # board_origin = (10, 20) - (1, -2) = (9, 22)
        # board_bottom_left = (9, 22 + 50) = (9, 72)
        # ref_bottom_left = (9, 72) + (5, 5) = (14, 77)
        ref = ReferencePoint(
            x=10.0,
            y=20.0,
            target_diameter=3.0,
            offsets=CornerOffsets(
                top_left=(1.0, -2.0),
                top_right=(1.0, -2.0),
                bottom_left=(5.0, 5.0),
            ),
        )

        assert ref.get_reference_position(
            Corner.BOTTOM_LEFT, board_height=50.0
        ) == Point2d(14.0, 77.0)

    def test_get_reference_position_bottom_left_requires_board_height(self):
        ref = ReferencePoint(
            x=10.0,
            y=20.0,
            target_diameter=3.0,
            offsets=CornerOffsets(
                top_left=(1.0, -2.0),
                top_right=(1.0, -2.0),
                bottom_left=(5.0, 5.0),
            ),
        )

        with pytest.raises(ValueError, match="board_heightが必要"):
            ref.get_reference_position(Corner.BOTTOM_LEFT)

    def test_get_reference_position_bottom_right(self):
        # board_origin = (10, 20) - (1, -2) = (9, 22)
        # board_bottom_right = (9 + 100, 22 + 50) = (109, 72)
        # ref_bottom_right = (109, 72) + (-5, 5) = (104, 77)
        ref = ReferencePoint(
            x=10.0,
            y=20.0,
            target_diameter=3.0,
            offsets=CornerOffsets(
                top_left=(1.0, -2.0),
                top_right=(1.0, -2.0),
                bottom_left=(5.0, 5.0),
                bottom_right=(-5.0, 5.0),
            ),
        )

        assert ref.get_reference_position(
            Corner.BOTTOM_RIGHT, board_width=100.0, board_height=50.0
        ) == Point2d(104.0, 77.0)


class TestGetMachineConfig:
    """get_machine_config関数のテスト."""

    _MINIMAL_TOML = """\
[klipper]

[camera]
width = 640
height = 480
fps = 30.0
calibration_file = "calibration.json"

[camera.crop]
width = 400
height = 400

[reference_point]
x = 20.0
y = 10.0
target_diameter = 3.0

[reference_point.offsets]
top_left = [0.0, 0.0]
top_right = [0.0, 0.0]
bottom_right = [0.0, 0.0]
"""

    def test_loads_machine_config(self, tmp_path, monkeypatch):
        config_dir = tmp_path / "configs" / "test_machine"
        config_dir.mkdir(parents=True)
        (config_dir / "machine.toml").write_text(self._MINIMAL_TOML)

        monkeypatch.setattr("pcb_assembly.config.PROJECT_ROOT", tmp_path)

        machine = get_machine_config("test_machine")

        assert isinstance(machine, Machine)
        assert machine.klipper == Klipper(host="localhost", port=7125)

    def test_raises_file_not_found_for_nonexistent_machine(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pcb_assembly.config.PROJECT_ROOT", tmp_path)

        with pytest.raises(FileNotFoundError):
            get_machine_config("nonexistent")
