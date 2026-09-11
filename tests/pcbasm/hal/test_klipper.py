import pytest
from pytest_mock import MockerFixture

from pcbasm.gcode import PRESENT_MACRO, GCode
from pcbasm.hal.klipper import GCodeMacro, Klipper
from tests.helpers import mark_hardware


class TestKlipper:
    """Klipperクラスのテスト."""

    @pytest.fixture
    def klipper(self) -> Klipper:
        return Klipper()

    @mark_hardware
    def test_send_gcode(self, klipper: Klipper):
        result = klipper.send_gcode("M115")

        assert "result" in result

    @mark_hardware
    def test_get_status(self, klipper: Klipper):
        homed_axes = klipper.get_status("toolhead", "homed_axes")

        assert isinstance(homed_axes, str)

    @mark_hardware
    def test_get_status_gcode_position(self, klipper: Klipper):
        position = klipper.get_status("gcode_move", "gcode_position")

        assert isinstance(position, list)
        assert len(position) >= 3  # X, Y, Zはあるはず

    @mark_hardware
    def test_get_config(self, klipper: Klipper):
        config = klipper.get_config()

        assert isinstance(config, dict)

    @mark_hardware
    def test_get_macros(self, klipper: Klipper):
        macros = klipper.get_macros()

        assert isinstance(macros, dict)
        for name, macro in macros.items():
            assert isinstance(name, str)
            assert isinstance(macro, GCodeMacro)

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
        assert macros["TEST_MACRO"] == GCodeMacro(
            gcode=GCode("G28"), description="テストマクロ"
        )
        assert macros["NO_DESC"] == GCodeMacro(gcode=GCode("M400"), description=None)

    def test_send_present_or_relax_uses_present_macro(self, mocker: MockerFixture):
        klipper = Klipper()
        has_macro = mocker.patch.object(klipper, "has_macro", return_value=True)
        send_gcode = mocker.patch.object(klipper, "send_gcode")

        klipper.send_present_or_relax()

        has_macro.assert_called_once_with(PRESENT_MACRO)
        send_gcode.assert_called_once()
        assert send_gcode.call_args.args == (GCode("PRESENT"),)
        assert send_gcode.call_args.kwargs["timeout"] > 10.0

    def test_send_present_or_relax_warns_and_relaxes_without_macro(
        self, mocker: MockerFixture
    ):
        klipper = Klipper()
        mocker.patch.object(klipper, "has_macro", return_value=False)
        send_gcode = mocker.patch.object(klipper, "send_gcode")
        warnings: list[str] = []

        klipper.send_present_or_relax(warn=warnings.append)

        send_gcode.assert_called_once()
        assert send_gcode.call_args.args == (GCode("M84"),)
        assert send_gcode.call_args.kwargs["timeout"] > 10.0
        assert len(warnings) == 1
        assert "PRESENT" in warnings[0]
        assert "M84" in warnings[0]

    def test_send_present_or_relax_warns_and_relaxes_when_macro_check_fails(
        self, mocker: MockerFixture
    ):
        klipper = Klipper()
        mocker.patch.object(
            klipper, "has_macro", side_effect=RuntimeError("config unavailable")
        )
        send_gcode = mocker.patch.object(klipper, "send_gcode")
        warnings: list[str] = []

        klipper.send_present_or_relax(warn=warnings.append)

        send_gcode.assert_called_once()
        assert send_gcode.call_args.args == (GCode("M84"),)
        assert send_gcode.call_args.kwargs["timeout"] > 10.0
        assert len(warnings) == 1
        assert "PRESENT" in warnings[0]
        assert "config unavailable" in warnings[0]
