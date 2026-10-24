import pytest

from pcbasm.hal.air_pump import AirPump
from pcbasm.hal.fake import FakeKlipper


class TestAirPump:
    @pytest.fixture
    def air_pump(self) -> AirPump:
        return AirPump(FakeKlipper({"output_pin air_pump": {}}))

    def test_init_missing_section(self):
        with pytest.raises(
            RuntimeError, match=r"printer\.cfgに\[output_pin air_pump\]"
        ):
            AirPump(FakeKlipper({}))

    @pytest.mark.parametrize(
        ("method", "value"), [("on", 1), ("off", 0)], ids=["on", "off"]
    )
    def test_on_off_sets_the_pin(self, air_pump: AirPump, method: str, value: int):
        gcode = getattr(air_pump, method)()

        assert gcode.to_list() == [f"SET_PIN PIN=air_pump VALUE={value}"]
