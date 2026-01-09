from collections.abc import Generator

import pytest

from pcb_assembly.hal.klipper import Klipper, Macro
from tests.helpers import mark_hardware


class TestKlipper:
    """Klipperクラスのテスト."""

    @pytest.fixture
    def klipper(self) -> Generator[Klipper]:
        """ホーミング済みKlipperインスタンスを提供し、終了時にリラックスする."""
        klipper = Klipper()
        with klipper.buffered():
            klipper.queue("G28")
            klipper.queue("M400")
        yield klipper
        with klipper.buffered():
            klipper.queue("M18")

    @mark_hardware
    def test_buffered_and_queue(self, klipper: Klipper):
        with klipper.buffered():
            klipper.queue("M115")
            klipper.queue("M400")

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

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("TEST_MACRO", True),
            ("NONEXISTENT", False),
        ],
    )
    def test_has_macro(self, mocker, name: str, expected: bool):
        klipper = Klipper()
        mocker.patch.object(
            klipper,
            "get_macros",
            return_value={"TEST_MACRO": Macro(gcode="G28")},
        )

        assert klipper.has_macro(name) == expected
