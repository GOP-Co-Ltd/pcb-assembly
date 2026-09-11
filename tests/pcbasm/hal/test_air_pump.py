import pytest
from pytest_mock import MockerFixture

from pcbasm.hal.air_pump import AirPump
from pcbasm.hal.klipper import Klipper
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

    @pytest.mark.parametrize(
        ("method", "value"), [("on", 1), ("off", 0)], ids=["on", "off"]
    )
    def test_on_off_sets_the_pin(self, air_pump: AirPump, method: str, value: int):
        gcode = getattr(air_pump, method)()

        assert gcode.to_list() == [f"SET_PIN PIN=air_pump VALUE={value}"]
