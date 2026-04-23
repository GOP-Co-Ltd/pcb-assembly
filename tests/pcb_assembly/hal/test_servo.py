import pytest
from pytest_mock import MockerFixture

from pcb_assembly.gcode import GCode
from pcb_assembly.hal.klipper import Klipper
from pcb_assembly.hal.servo import Servo
from tests.helpers import mark_hardware


class TestServo:
    @pytest.fixture
    def mock_klipper(self, mocker: MockerFixture):
        klipper = Klipper()
        mocker.patch.object(
            klipper.readonly,
            "get_config",
            return_value={"servo probe_gnd": {}},
        )
        return klipper

    @pytest.fixture
    def servo(self, mock_klipper: Klipper) -> Servo:
        return Servo(mock_klipper.readonly, "probe_gnd")

    @mark_hardware
    def test_init(self):
        klipper = Klipper()
        Servo(klipper.readonly, "probe_gnd")

    def test_init_missing_section(self, mocker: MockerFixture):
        klipper = Klipper()
        mocker.patch.object(klipper.readonly, "get_config", return_value={})
        with pytest.raises(RuntimeError, match=r"printer\.cfgに\[servo probe_gnd\]"):
            Servo(klipper.readonly, "probe_gnd")

    def test_set_angle(self, servo: Servo):
        gcode = servo.set_angle(90)
        assert isinstance(gcode, GCode)
        assert gcode.to_list() == ["SET_SERVO SERVO=probe_gnd ANGLE=90"]

    @pytest.mark.parametrize("angle", [0, 45, 90, 180])
    def test_set_angle_parametrized(self, servo: Servo, angle: float):
        gcode = servo.set_angle(angle)
        assert isinstance(gcode, GCode)
        assert gcode.to_list() == [f"SET_SERVO SERVO=probe_gnd ANGLE={angle}"]

    def test_custom_name(self, mocker: MockerFixture):
        klipper = Klipper()
        mocker.patch.object(
            klipper.readonly,
            "get_config",
            return_value={"servo other": {}},
        )
        servo = Servo(klipper.readonly, "other")
        gcode = servo.set_angle(45)
        assert gcode.to_list() == ["SET_SERVO SERVO=other ANGLE=45"]

    def test_custom_name_missing_section(self, mocker: MockerFixture):
        klipper = Klipper()
        mocker.patch.object(klipper.readonly, "get_config", return_value={})
        with pytest.raises(RuntimeError, match=r"printer\.cfgに\[servo other\]"):
            Servo(klipper.readonly, "other")
