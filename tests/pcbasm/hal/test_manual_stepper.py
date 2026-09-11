import pytest
from pytest_mock import MockerFixture

from pcbasm.hal.klipper import Klipper
from pcbasm.hal.manual_stepper import HomingDirection, ManualStepper

STEPPER_NAME = "vacuum_pump"
PREFIX = f"MANUAL_STEPPER STEPPER={STEPPER_NAME}"


class TestManualStepper:
    """ManualStepperクラスのテスト."""

    @pytest.fixture
    def mock_klipper(self, mocker: MockerFixture):
        """Klipperをモックするフィクスチャ."""
        klipper = Klipper()
        mocker.patch.object(
            klipper.readonly,
            "get_config",
            return_value={
                f"manual_stepper {STEPPER_NAME}": {"rotation_distance": "0.5"}
            },
        )
        return klipper

    @pytest.fixture
    def stepper(self, mock_klipper: Klipper) -> ManualStepper:
        return ManualStepper(mock_klipper.readonly, STEPPER_NAME)

    def test_init_missing_stepper_section(self, mocker: MockerFixture):
        klipper = Klipper()
        mocker.patch.object(klipper.readonly, "get_config", return_value={})

        with pytest.raises(
            RuntimeError, match=r"printer\.cfgに\[manual_stepper vacuum_pump\]"
        ):
            ManualStepper(klipper.readonly, STEPPER_NAME)

    def test_rotation_distance(self, stepper: ManualStepper):
        assert stepper.rotation_distance == 0.5

    @pytest.mark.parametrize(
        ("angle", "speed", "accel", "sync", "expected_suffix"),
        [
            # rotation_distance=0.5, so 1deg = 0.5/360 mm
            (360.0, None, None, True, "MOVE=0.5"),
            (180.0, 360.0, None, True, "MOVE=0.25 SPEED=0.5"),
            (360.0, 720.0, 3600.0, True, "MOVE=0.5 SPEED=1.0 ACCEL=5.0"),
            (360.0, None, None, False, "MOVE=0.5 SYNC=0"),
        ],
    )
    def test_rotate(
        self,
        stepper: ManualStepper,
        angle: float,
        speed: float | None,
        accel: float | None,
        sync: bool,
        expected_suffix: str,
    ):
        gcode = stepper.rotate(angle, speed, accel, sync=sync)
        assert gcode.to_list() == [f"{PREFIX} {expected_suffix}"]

    @pytest.mark.parametrize(
        ("position", "expected_pos"),
        [
            (None, "0.0"),
            (5.0, "5.0"),
        ],
    )
    def test_reset_position(
        self, stepper: ManualStepper, position: float | None, expected_pos: str
    ):
        gcode = (
            stepper.reset_position()
            if position is None
            else stepper.reset_position(position)
        )
        assert gcode.to_list() == [f"{PREFIX} SET_POSITION={expected_pos}"]

    @pytest.mark.parametrize(
        ("distance", "speed", "accel", "sync", "expected_suffix"),
        [
            (10.0, None, None, True, "MOVE=10.0"),
            (-5.0, None, None, True, "MOVE=-5.0"),
            (10.0, 5.0, 20.0, True, "MOVE=10.0 SPEED=5.0 ACCEL=20.0"),
            (10.0, None, None, False, "MOVE=10.0 SYNC=0"),
        ],
    )
    def test_move(
        self,
        stepper: ManualStepper,
        distance: float,
        speed: float | None,
        accel: float | None,
        sync: bool,
        expected_suffix: str,
    ):
        gcode = stepper.move(distance, speed, accel, sync=sync)
        assert gcode.to_list() == [f"{PREFIX} {expected_suffix}"]

    def test_sync(self, stepper: ManualStepper):
        assert stepper.sync().to_list() == [f"{PREFIX} SYNC=1"]

    @pytest.mark.parametrize(
        ("distance", "speed", "direction", "expected_suffix"),
        [
            (100.0, None, HomingDirection.FORWARD, "STOP_ON_ENDSTOP=1 MOVE=100.0"),
            (
                100.0,
                10.0,
                HomingDirection.BACKWARD,
                "STOP_ON_ENDSTOP=-1 MOVE=100.0 SPEED=10.0",
            ),
            (
                50.0,
                5.0,
                HomingDirection.FORWARD,
                "STOP_ON_ENDSTOP=1 MOVE=50.0 SPEED=5.0",
            ),
        ],
    )
    def test_home(
        self,
        stepper: ManualStepper,
        distance: float,
        speed: float | None,
        direction: HomingDirection,
        expected_suffix: str,
    ):
        gcode = stepper.home(distance, speed, direction=direction)
        assert gcode.to_list() == [f"{PREFIX} {expected_suffix}"]

    def test_enable(self, stepper: ManualStepper):
        gcode = stepper.enable()
        assert gcode.to_list() == [f"{PREFIX} ENABLE=1"]

    def test_disable(self, stepper: ManualStepper):
        gcode = stepper.disable()
        assert gcode.to_list() == [f"{PREFIX} ENABLE=0"]
