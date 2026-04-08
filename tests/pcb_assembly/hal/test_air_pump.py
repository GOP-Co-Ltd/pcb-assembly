import pytest
from pytest_mock import MockerFixture

from pcb_assembly.gcode import GCode
from pcb_assembly.hal.air_pump import AirPump
from pcb_assembly.hal.klipper import Klipper
from tests.helpers import mark_hardware


class TestAirPump:
    @pytest.fixture
    def mock_klipper(self, mocker: MockerFixture):
        klipper = Klipper()
        mocker.patch.object(
            klipper.readonly,
            "get_config",
            return_value={"output_pin air_pump": {}},
        )
        return klipper

    @pytest.fixture
    def air_pump(self, mock_klipper: Klipper) -> AirPump:
        return AirPump(mock_klipper.readonly)

    @mark_hardware
    def test_init(self):
        klipper = Klipper()
        AirPump(klipper.readonly)

    def test_init_missing_section(self, mocker: MockerFixture):
        klipper = Klipper()
        mocker.patch.object(klipper.readonly, "get_config", return_value={})
        with pytest.raises(
            RuntimeError, match=r"printer\.cfgに\[output_pin air_pump\]"
        ):
            AirPump(klipper.readonly)

    def test_on(self, air_pump: AirPump):
        gcode = air_pump.on()
        assert isinstance(gcode, GCode)
        assert gcode.to_list() == ["SET_PIN PIN=air_pump VALUE=1"]

    def test_off(self, air_pump: AirPump):
        gcode = air_pump.off()
        assert isinstance(gcode, GCode)
        assert gcode.to_list() == ["SET_PIN PIN=air_pump VALUE=0"]
