import math

import pytest
from pytest_mock import MockerFixture

from pcb_assembly.hal.klipper import GCode, Klipper
from pcb_assembly.hal.paste_dispenser import PasteDispenser
from tests.helpers import mark_hardware


class TestPasteDispenser:
    """PasteDispenserクラスのテスト."""

    @pytest.fixture
    def mock_klipper(self, mocker: MockerFixture):
        """Klipperをモックするフィクスチャ."""
        klipper = Klipper()
        mocker.patch.object(
            klipper.readonly,
            "get_config",
            return_value={"manual_stepper paste_dispenser": {}},
        )
        return klipper

    @pytest.fixture
    def dispenser(self, mock_klipper: Klipper) -> PasteDispenser:
        return PasteDispenser(mock_klipper.readonly, syringe_size=10.0)

    @mark_hardware
    def test_init(self):
        klipper = Klipper()
        PasteDispenser(klipper.readonly, syringe_size=10.0)

    def test_init_missing_stepper_section(self, mocker: MockerFixture):
        klipper = Klipper()
        mocker.patch.object(klipper.readonly, "get_config", return_value={})

        with pytest.raises(
            RuntimeError, match=r"printer\.cfgに\[manual_stepper paste_dispenser\]"
        ):
            PasteDispenser(klipper.readonly, syringe_size=10.0)

    def test_dispense(self, mock_klipper: Klipper):
        syringe_diameter = 10.0  # mm
        dispenser = PasteDispenser(mock_klipper.readonly, syringe_size=syringe_diameter)
        syringe_area = math.pi * (syringe_diameter / 2) ** 2

        rate = syringe_area  # μL/sec → 1mm/sec
        accel = syringe_area * 2  # μL/sec² → 2mm/sec²

        gcode = dispenser.dispense(rate, accel)

        assert isinstance(gcode, GCode)
        lines = gcode.to_list()
        assert lines[0] == "MANUAL_STEPPER STEPPER=paste_dispenser SET_POSITION=0"
        assert (
            lines[1]
            == "MANUAL_STEPPER STEPPER=paste_dispenser MOVE=1000 SPEED=1.0 ACCEL=2.0"
        )

    def test_retract(self, mock_klipper: Klipper):
        syringe_diameter = 10.0  # mm
        dispenser = PasteDispenser(mock_klipper.readonly, syringe_size=syringe_diameter)
        syringe_area = math.pi * (syringe_diameter / 2) ** 2

        amount = syringe_area  # μL → 1mm
        rate = syringe_area  # μL/sec → 1mm/sec
        accel = syringe_area * 2  # μL/sec² → 2mm/sec²

        gcode = dispenser.retract(amount, rate, accel)

        assert isinstance(gcode, GCode)
        lines = gcode.to_list()
        assert lines[0] == "MANUAL_STEPPER STEPPER=paste_dispenser SET_POSITION=0"
        assert (
            lines[1]
            == "MANUAL_STEPPER STEPPER=paste_dispenser MOVE=-1.0 SPEED=1.0 ACCEL=2.0"
        )

    def test_stop(self, dispenser: PasteDispenser):
        gcode = dispenser.stop()

        assert isinstance(gcode, GCode)
        lines = gcode.to_list()
        assert lines[0] == "MANUAL_STEPPER STEPPER=paste_dispenser ENABLE=0"
        assert lines[1] == "MANUAL_STEPPER STEPPER=paste_dispenser ENABLE=1"
