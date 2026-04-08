import pytest
from pytest_mock import MockerFixture

from pcb_assembly.gcode import GCode
from pcb_assembly.hal.klipper import Klipper
from pcb_assembly.hal.paste_dispenser import (
    NOZZLE_SPECS,
    NozzleSpec,
    PasteDispenser,
)
from tests.helpers import mark_hardware

STEPPER_NAME = "paste_dispenser"
PREFIX = f"MANUAL_STEPPER STEPPER={STEPPER_NAME}"


class TestNozzleSpec:
    """NozzleSpecクラスのテスト."""

    def test_nozzle_spec(self):
        spec = NozzleSpec(inner_diameter=0.19)
        assert spec.inner_diameter == 0.19

    def test_nozzle_specs_27g(self):
        assert "27G" in NOZZLE_SPECS
        assert NOZZLE_SPECS["27G"].inner_diameter == 0.19


class TestPasteDispenser:
    """PasteDispenserクラスのテスト."""

    @pytest.fixture
    def mock_klipper(self, mocker: MockerFixture):
        """Klipperをモックするフィクスチャ."""
        klipper = Klipper()
        mocker.patch.object(
            klipper.readonly,
            "get_config",
            return_value={
                f"manual_stepper {STEPPER_NAME}": {"rotation_distance": "0.5"},
                "output_pin air_pump": {},
            },
        )
        return klipper

    @mark_hardware
    def test_init(self):
        klipper = Klipper()
        PasteDispenser(klipper.readonly, rotations_per_ul=0.5)

    def test_init_missing_stepper_section(self, mocker: MockerFixture):
        klipper = Klipper()
        mocker.patch.object(
            klipper.readonly,
            "get_config",
            return_value={"output_pin air_pump": {}},
        )

        with pytest.raises(
            RuntimeError, match=r"printer\.cfgに\[manual_stepper paste_dispenser\]"
        ):
            PasteDispenser(klipper.readonly, rotations_per_ul=0.5)

    def test_init_missing_air_pump_section(self, mocker: MockerFixture):
        klipper = Klipper()
        mocker.patch.object(
            klipper.readonly,
            "get_config",
            return_value={
                f"manual_stepper {STEPPER_NAME}": {"rotation_distance": "0.5"},
            },
        )

        with pytest.raises(
            RuntimeError, match=r"printer\.cfgに\[output_pin air_pump\]"
        ):
            PasteDispenser(klipper.readonly, rotations_per_ul=0.5)

    def test_enable(self, mock_klipper: Klipper):
        dispenser = PasteDispenser(mock_klipper.readonly, rotations_per_ul=0.5)
        gcode = dispenser.enable()

        assert isinstance(gcode, GCode)
        lines = gcode.to_list()
        assert lines[0] == "SET_PIN PIN=air_pump VALUE=1"
        assert lines[1] == f"{PREFIX} ENABLE=1"

    def test_disable(self, mock_klipper: Klipper):
        dispenser = PasteDispenser(mock_klipper.readonly, rotations_per_ul=0.5)
        gcode = dispenser.disable()

        assert isinstance(gcode, GCode)
        lines = gcode.to_list()
        assert lines[0] == "SET_PIN PIN=air_pump VALUE=0"
        assert lines[1] == f"{PREFIX} ENABLE=0"

    @pytest.mark.parametrize(
        ("amount", "expected_move_sign"),
        [
            (1.0, ""),  # 正: 吐出
            (-1.0, "-"),  # 負: リトラクション
        ],
    )
    def test_pushpull(
        self, mock_klipper: Klipper, amount: float, expected_move_sign: str
    ):
        # rotations_per_ul=0.5, rotation_distance=0.5
        # 1μL → 0.5rev → 180deg → 180/360 * 0.5mm = 0.25mm
        rotations_per_ul = 0.5
        dispenser = PasteDispenser(
            mock_klipper.readonly, rotations_per_ul=rotations_per_ul
        )

        rate = 2.0  # μL/sec → 0.5*360*2 = 360 deg/s → 0.5mm/s
        accel = 4.0  # μL/sec² → 0.5*360*4 = 720 deg/s² → 1.0mm/s²

        gcode = dispenser.pushpull(amount, rate, accel)

        assert isinstance(gcode, GCode)
        lines = gcode.to_list()
        assert lines[0] == f"{PREFIX} SET_POSITION=0.0"
        assert lines[1] == f"{PREFIX} MOVE={expected_move_sign}0.25 SPEED=0.5 ACCEL=1.0"

    def test_pushpull_sync_false(self, mock_klipper: Klipper):
        dispenser = PasteDispenser(mock_klipper.readonly, rotations_per_ul=0.5)
        gcode = dispenser.pushpull(1.0, 2.0, 4.0, sync=False)

        lines = gcode.to_list()
        assert lines[1].endswith("SYNC=0")
