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

    def test_add_does_not_modify_original(self):
        a = GCode("G28")
        b = GCode("M400")
        _ = a + b
        assert a.to_list() == ["G28"]


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


class TestGCodeWaitForDone:
    """GCode.wait_for_done のテスト."""

    def test_wait_for_done(self):
        assert str(GCode.wait_for_done()) == "M400"


class TestGCodePresent:
    """GCode.present のテスト."""

    def test_present(self):
        assert str(GCode.present()) == "PRESENT"


class TestGCodeFirmwareRestart:
    """GCode.firmware_restart のテスト."""

    def test_firmware_restart(self):
        assert GCode.firmware_restart() == GCode("FIRMWARE_RESTART")


class TestGCodeRelax:
    """GCode.relax のテスト."""

    def test_relax(self):
        assert str(GCode.relax()) == "M84"
