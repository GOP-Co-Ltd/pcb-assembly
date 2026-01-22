import pytest

from pcb_assembly.gcode import GCode


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

    def test_eq_returns_not_implemented_for_non_gcode(self):
        gcode = GCode("G28")

        assert gcode.__eq__("G28") == NotImplemented

    def test_hash(self):
        gcode1 = GCode("G28")
        gcode2 = GCode("G28")
        gcode3 = GCode("M400")

        assert hash(gcode1) == hash(gcode2)
        assert hash(gcode1) != hash(gcode3)

    def test_hash_usable_in_set(self):
        gcode_set = {GCode("G28"), GCode("G28"), GCode("M400")}

        assert len(gcode_set) == 2
