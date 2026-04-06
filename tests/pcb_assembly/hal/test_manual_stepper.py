import pytest
from pytest_mock import MockerFixture

from pcb_assembly.gcode import GCode
from pcb_assembly.hal.klipper import Klipper
from pcb_assembly.hal.manual_stepper import HomingDirection, ManualStepper
from tests.helpers import mark_hardware

STEPPER_NAME = "vacuum_pump"


class TestManualStepper:
    """ManualStepperクラスのテスト."""

    @pytest.fixture
    def mock_klipper(self, mocker: MockerFixture):
        """Klipperをモックするフィクスチャ."""
        klipper = Klipper()
        mocker.patch.object(
            klipper.readonly,
            "get_config",
            return_value={f"manual_stepper {STEPPER_NAME}": {}},
        )
        return klipper

    @pytest.fixture
    def stepper(self, mock_klipper: Klipper) -> ManualStepper:
        return ManualStepper(mock_klipper.readonly, STEPPER_NAME)

    @mark_hardware
    def test_init(self):
        klipper = Klipper()
        ManualStepper(klipper.readonly, STEPPER_NAME)

    def test_init_missing_stepper_section(self, mocker: MockerFixture):
        klipper = Klipper()
        mocker.patch.object(klipper.readonly, "get_config", return_value={})

        with pytest.raises(
            RuntimeError, match=r"printer\.cfgに\[manual_stepper vacuum_pump\]"
        ):
            ManualStepper(klipper.readonly, STEPPER_NAME)

    def test_name(self, stepper: ManualStepper):
        assert stepper.name == STEPPER_NAME

    def test_reset_position_default(self, stepper: ManualStepper):
        gcode = stepper.reset_position()
        assert isinstance(gcode, GCode)
        assert gcode.to_list() == [
            f"MANUAL_STEPPER STEPPER={STEPPER_NAME} SET_POSITION=0.0"
        ]

    def test_reset_position_nonzero(self, stepper: ManualStepper):
        gcode = stepper.reset_position(5.0)
        assert gcode.to_list() == [
            f"MANUAL_STEPPER STEPPER={STEPPER_NAME} SET_POSITION=5.0"
        ]

    def test_move_distance_only(self, stepper: ManualStepper):
        gcode = stepper.move(10.0)
        assert gcode.to_list() == [f"MANUAL_STEPPER STEPPER={STEPPER_NAME} MOVE=10.0"]

    def test_move_with_speed_and_accel(self, stepper: ManualStepper):
        gcode = stepper.move(10.0, 5.0, 20.0)
        assert gcode.to_list() == [
            f"MANUAL_STEPPER STEPPER={STEPPER_NAME} MOVE=10.0 SPEED=5.0 ACCEL=20.0"
        ]

    def test_move_sync_false(self, stepper: ManualStepper):
        gcode = stepper.move(10.0, sync=False)
        assert gcode.to_list() == [
            f"MANUAL_STEPPER STEPPER={STEPPER_NAME} MOVE=10.0 SYNC=0"
        ]

    def test_move_negative_distance(self, stepper: ManualStepper):
        gcode = stepper.move(-5.0)
        assert gcode.to_list() == [f"MANUAL_STEPPER STEPPER={STEPPER_NAME} MOVE=-5.0"]

    def test_home_forward(self, stepper: ManualStepper):
        gcode = stepper.home(100.0)
        assert gcode.to_list() == [
            f"MANUAL_STEPPER STEPPER={STEPPER_NAME} STOP_ON_ENDSTOP=1 MOVE=100.0"
        ]

    def test_home_backward_with_speed(self, stepper: ManualStepper):
        gcode = stepper.home(100.0, 10.0, direction=HomingDirection.BACKWARD)
        assert gcode.to_list() == [
            f"MANUAL_STEPPER STEPPER={STEPPER_NAME} STOP_ON_ENDSTOP=-1 MOVE=100.0 SPEED=10.0"
        ]

    def test_home_forward_with_speed(self, stepper: ManualStepper):
        gcode = stepper.home(50.0, 5.0)
        assert gcode.to_list() == [
            f"MANUAL_STEPPER STEPPER={STEPPER_NAME} STOP_ON_ENDSTOP=1 MOVE=50.0 SPEED=5.0"
        ]

    def test_enable(self, stepper: ManualStepper):
        gcode = stepper.enable()
        assert gcode.to_list() == [f"MANUAL_STEPPER STEPPER={STEPPER_NAME} ENABLE=1"]

    def test_disable(self, stepper: ManualStepper):
        gcode = stepper.disable()
        assert gcode.to_list() == [f"MANUAL_STEPPER STEPPER={STEPPER_NAME} ENABLE=0"]
