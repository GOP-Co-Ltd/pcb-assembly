from pathlib import Path

import pytest

from pcb_assembly.config import (
    Camera,
    CameraCrop,
    Klipper,
    Machine,
    PasteDispenser,
    Probe,
    ReferencePoint,
    Toolhead,
)
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
