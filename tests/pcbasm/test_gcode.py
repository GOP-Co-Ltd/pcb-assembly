import pytest

from pcbasm.gcode import (
    GCode,
    firmware_restart,
    homing,
    move,
    move_to_cap,
    present,
    relax,
    wait,
    wait_for_done,
)


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


class TestHoming:
    """homing関数のテスト."""

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
        assert str(homing(**kwargs)) == expected


class TestMove:
    """move関数のテスト."""

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
        assert str(move(**kwargs)) == expected


class TestWait:
    """wait関数のテスト."""

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
        assert str(wait(seconds)) == expected


class TestWaitForDone:
    """wait_for_done関数のテスト."""

    def test_wait_for_done(self):
        assert str(wait_for_done()) == "M400"


class TestPresent:
    """present関数のテスト."""

    def test_present(self):
        assert str(present()) == "PRESENT"


class TestMoveToCap:
    """move_to_cap関数のテスト（nozzle-cap-parking 計画書「G-code 列」節が契約）.

    「Z を 0 へ → キャップ XY へ → キャップ Z へ」の 3 段シーケンス。 中間セグメントに Z ワードを含めない（Z
    先行の意味を壊さない）。 M400 / M84 は含めない（終了時経路が後置で合成する）。
    """

    def test_sequence_is_g90_then_z0_then_xy_then_z_at_present_speed(self):
        result = move_to_cap(10.0, 20.0, 3.5)

        assert result.to_list() == [
            "G90",
            "G1 Z0.0 F1200.0",
            "G1 X10.0 Y20.0 F1200.0",
            "G1 Z3.5 F1200.0",
        ]

    def test_sequence_contains_no_m400_or_m84(self):
        lines = move_to_cap(10.0, 20.0, 3.5).to_list()

        assert "M400" not in lines
        assert "M84" not in lines

    def test_velocity_override_changes_feedrate(self):
        result = move_to_cap(1.0, 2.0, 3.0, velocity=30.0)

        assert result.to_list() == [
            "G90",
            "G1 Z0.0 F1800.0",
            "G1 X1.0 Y2.0 F1800.0",
            "G1 Z3.0 F1800.0",
        ]


class TestFirmwareRestart:
    """firmware_restart関数のテスト."""

    def test_firmware_restart(self):
        assert firmware_restart() == GCode("FIRMWARE_RESTART")


class TestRelax:
    """relax関数のテスト."""

    def test_relax(self):
        assert str(relax()) == "M84"
