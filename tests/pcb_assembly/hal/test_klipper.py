import pytest
from pytest_mock import MockerFixture

from pcb_assembly.hal.klipper import GCode, Klipper, Macro, ReadonlyKlipper
from tests.helpers import mark_hardware


class TestGCode:
    """GCodeクラスのテスト."""

    @pytest.mark.parametrize(
        ("input", "expected"),
        [
            (None, []),
            ("G28", ["G28"]),
            (["G28", "M400"], ["G28", "M400"]),
        ],
    )
    def test_init(self, input, expected):
        gcode = GCode(input)
        assert gcode.to_list() == expected

    def test_init_gcode_copies(self):
        original = GCode(["G28", "M400"])
        copied = GCode(original)
        copied.append("G0 X10")
        assert original.to_list() == ["G28", "M400"]

    def test_str(self):
        gcode = GCode(["G28", "M400"])
        assert str(gcode) == "G28\nM400"

    def test_repr(self):
        gcode = GCode("G28")
        assert repr(gcode) == "GCode(G28)"

    def test_copy(self):
        original = GCode(["G28", "M400"])
        copied = original.copy()
        copied.append("G0 X10")
        assert original.to_list() == ["G28", "M400"]

    def test_to_list_returns_copy(self):
        gcode = GCode(["G28"])
        result = gcode.to_list()
        result.append("M400")
        assert gcode.to_list() == ["G28"]

    @pytest.mark.parametrize(
        ("input", "expected"),
        [
            ("M400", ["G28", "M400"]),
            (["M400", "G0 X10"], ["G28", "M400", "G0 X10"]),
            (GCode(["M400", "G0 X10"]), ["G28", "M400", "G0 X10"]),
        ],
    )
    def test_append(self, input, expected):
        gcode = GCode("G28")
        gcode.append(input)
        assert gcode.to_list() == expected


class TestKlipper:
    """Klipperクラスのテスト."""

    @pytest.fixture
    def klipper(self) -> Klipper:
        return Klipper()

    def test_readonly_property(self, klipper: Klipper):
        assert isinstance(klipper.readonly, ReadonlyKlipper)

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


class TestReadonlyKlipper:
    """ReadonlyKlipperのテスト."""

    @pytest.fixture
    def klipper(self, mocker: MockerFixture):
        mocker.patch("httpx.Client")
        return Klipper()

    @pytest.fixture
    def readonly(self, klipper):
        return ReadonlyKlipper(klipper)

    def test_exposed_method_equals_to_klipper(
        self, klipper: Klipper, readonly: ReadonlyKlipper
    ):
        assert readonly.get_config == klipper.get_config
        assert readonly.get_macros == klipper.get_macros
        assert readonly.get_status == klipper.get_status
        assert readonly.has_macro == klipper.has_macro
