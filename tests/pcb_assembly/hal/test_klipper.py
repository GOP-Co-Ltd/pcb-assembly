import pytest

from pcb_assembly.hal.klipper import Klipper, Macro
from tests.helpers import mark_hardware


@pytest.fixture
def klipper():
    """ホーミング済みKlipperインスタンスを提供し、終了時にリラックスする."""
    klipper = Klipper()
    klipper.home()
    yield klipper
    klipper.relax()


class TestKlipper:
    """Klipperクラスのテスト."""

    @mark_hardware
    def test_send_gcode(self, klipper):
        result = klipper.send_gcode("M115")

        assert "result" in result

    @mark_hardware
    def test_wait_for_move(self, klipper):
        # M400は例外を発生させずに完了すべき
        klipper.wait_for_move()

    @mark_hardware
    def test_get_status(self, klipper):
        homed_axes = klipper.get_status("toolhead", "homed_axes")

        assert isinstance(homed_axes, str)

    @mark_hardware
    def test_get_status_gcode_position(self, klipper):
        position = klipper.get_status("gcode_move", "gcode_position")

        assert isinstance(position, list)
        assert len(position) >= 3  # X, Y, Zはあるはず

    @mark_hardware
    def test_get_config(self, klipper):
        config = klipper.get_config()

        assert isinstance(config, dict)

    @mark_hardware
    def test_get_macros(self, klipper):
        macros = klipper.get_macros()

        assert isinstance(macros, dict)
        for name, macro in macros.items():
            assert isinstance(name, str)
            assert isinstance(macro, Macro)

    def test_get_macros_parses_config(self, mocker):
        klipper = Klipper()
        mocker.patch.object(
            klipper,
            "get_config",
            return_value={
                "gcode_macro TEST_MACRO": {
                    "gcode": "G28",
                    "description": "テストマクロ",
                },
                "gcode_macro NO_DESC": {
                    "gcode": "M400",
                },
                "stepper_x": {"step_pin": "PC0"},
            },
        )

        macros = klipper.get_macros()

        assert len(macros) == 2
        assert macros["TEST_MACRO"] == Macro(gcode="G28", description="テストマクロ")
        assert macros["NO_DESC"] == Macro(gcode="M400", description=None)
