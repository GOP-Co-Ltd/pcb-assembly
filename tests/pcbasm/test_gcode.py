import pytest

from pcbasm.gcode import GCode


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

    @pytest.mark.parametrize(
        "derive",
        [
            GCode,
            lambda original: original.copy(),
            lambda original: original.to_list(),
            lambda original: original + "G0 X10",
        ],
        ids=["init-from-gcode", "copy", "to_list", "add"],
    )
    def test_derived_value_does_not_share_state_with_the_original(self, derive):
        original = GCode(["G28", "M400"])

        derive(original).append("G0 X10")

        assert original.to_list() == ["G28", "M400"]

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

    @pytest.mark.parametrize(
        ("gcode1", "gcode2", "expected"),
        [
            (GCode("G28"), GCode("G28"), True),
            (GCode(["G28", "M400"]), GCode(["G28", "M400"]), True),
            (GCode("G28"), GCode("M400"), False),
            (GCode(["G28"]), GCode(["G28", "M400"]), False),
        ],
    )
    def test_eq(self, gcode1: GCode, gcode2: GCode, expected: bool):
        assert (gcode1 == gcode2) == expected

    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            (GCode("G28"), GCode("M400"), ["G28", "M400"]),
            (GCode("G28"), "M400", ["G28", "M400"]),
            (GCode("G28"), ["M400", "G0 X10"], ["G28", "M400", "G0 X10"]),
        ],
    )
    def test_add(self, a, b, expected):
        result = a + b
        assert result.to_list() == expected


class TestGCodeHoming:
    """GCode.homing のテスト."""

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({}, "G28"),
            ({"x": True}, "G28 X"),
            ({"y": True}, "G28 Y"),
            ({"z": True}, "G28 Z"),
            ({"x": True, "y": True}, "G28 X Y"),
            ({"x": True, "y": True, "z": True}, "G28 X Y Z"),
        ],
    )
    def test_homing(self, kwargs, expected):
        assert str(GCode.homing(**kwargs)) == expected


class TestGCodeMove:
    """GCode.move のテスト."""

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({}, ""),
            ({"x": 10}, "G1 X10"),
            ({"y": 20}, "G1 Y20"),
            ({"z": 5}, "G1 Z5"),
            ({"x": 10, "y": 20}, "G1 X10 Y20"),
            ({"x": 10, "y": 20, "z": 5}, "G1 X10 Y20 Z5"),
            ({"velocity": 10}, "G1 F600"),
            ({"x": 10, "velocity": 10}, "G1 X10 F600"),
        ],
    )
    def test_move(self, kwargs, expected):
        assert str(GCode.move(**kwargs)) == expected


class TestGCodeWait:
    """GCode.wait のテスト."""

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, ""),
            (1, "G4 P1000"),
            (0.5, "G4 P500"),
            (2.5, "G4 P2500"),
        ],
    )
    def test_wait(self, seconds, expected):
        assert str(GCode.wait(seconds)) == expected


class TestGCodeMacros:
    """引数を取らない定数マクロのテスト."""

    @pytest.mark.parametrize(
        ("factory", "expected"),
        [
            (GCode.wait_for_done, "M400"),
            (GCode.present, "PRESENT"),
            (GCode.firmware_restart, "FIRMWARE_RESTART"),
            (GCode.relax, "M84"),
        ],
        ids=["wait_for_done", "present", "firmware_restart", "relax"],
    )
    def test_macro(self, factory, expected):
        assert str(factory()) == expected
