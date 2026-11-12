"""`pcbasm.machine.motion_commands` の仕様テスト（新ファームの機体の操作パネルの命令）."""

import pytest

from pcbasm.geometry import Point3d
from pcbasm.hal import Limits, MotionState, MotionStatus, ScalarLimits, Velocity
from pcbasm.hal.fake import FakeMotionStage
from pcbasm.machine.commands import (
    FocusZ,
    Home,
    Jog,
    MachineCommand,
    Move,
    MoveToCap,
    RawGCode,
    Relax,
)
from pcbasm.machine.motion_commands import (
    execute_motion_command,
    motion_homed_axes,
    motion_position_label,
)

# kurousagi の可動域（Z は -40〜0 で、原点出しは正の向き）
LIMITS = Limits(
    x=ScalarLimits(0.0, 68.0),
    y=ScalarLimits(0.0, 50.0),
    z=ScalarLimits(-40.0, 0.0),
    v=ScalarLimits(0.0, 100.0),
)
MAX_VELOCITY = Velocity(100.0, 100.0, 50.0)


def _stage(*, homed: bool = False) -> FakeMotionStage:
    return FakeMotionStage(LIMITS, MAX_VELOCITY, homed=homed)


def _status(position: Point3d, homed: str) -> MotionStatus:
    return MotionStatus(
        position=position,
        velocity=Velocity(),
        state=MotionState.IDLE,
        homed=frozenset(homed),  # type: ignore[arg-type]
    )


class TestHome:
    def test_without_axes_homes_all_axes_z_first(self):
        stage = _stage()

        execute_motion_command(Home(), stage)

        assert stage.commands == ["home z", "home x", "home y"]
        assert stage.status().homed == {"x", "y", "z"}

    def test_homes_z_first_when_z_is_not_homed(self):
        stage = _stage()

        execute_motion_command(Home(axes=("x",)), stage)

        assert stage.commands == ["home z", "home x"]


class TestJog:
    def test_moves_one_axis_from_the_current_position(self):
        stage = _stage(homed=True)
        execute_motion_command(Move(x=10.0, z=-5.0), stage)

        execute_motion_command(Jog(axis="x", distance=2.5), stage)
        execute_motion_command(Jog(axis="z", distance=-1.0), stage)

        assert stage.status().position == Point3d(12.5, 0.0, -6.0)

    def test_outside_the_limits_is_rejected_without_moving(self):
        stage = _stage(homed=True)

        with pytest.raises(ValueError, match="可動域"):
            execute_motion_command(Jog(axis="z", distance=1.0), stage)

        assert stage.commands == []

    def test_unhomed_axis_is_rejected(self):
        stage = _stage()

        with pytest.raises(ValueError, match="原点"):
            execute_motion_command(Jog(axis="y", distance=1.0), stage)

        assert stage.commands == []


class TestMove:
    def test_moves_the_given_axes(self):
        stage = _stage(homed=True)

        execute_motion_command(Move(x=30.0, y=20.0), stage)

        assert stage.status().position == Point3d(30.0, 20.0, 0.0)
        assert stage.commands == ["move_to"]

    def test_outside_the_limits_is_rejected(self):
        stage = _stage(homed=True)

        with pytest.raises(ValueError, match="可動域"):
            execute_motion_command(Move(z=-50.0), stage)

    def test_unhomed_axis_is_rejected(self):
        stage = _stage()

        with pytest.raises(ValueError, match="原点"):
            execute_motion_command(Move(x=1.0), stage)


class TestRelax:
    def test_relaxes_and_forgets_the_origin(self):
        stage = _stage(homed=True)

        execute_motion_command(Relax(), stage)

        assert stage.commands == ["relax"]
        assert stage.status().homed == frozenset()


class TestUnavailable:
    @pytest.mark.parametrize(
        "command", [FocusZ(), MoveToCap(), RawGCode(text="G28")], ids=repr
    )
    def test_klipper_only_commands_are_rejected(self, command: MachineCommand):
        stage = _stage(homed=True)

        with pytest.raises(ValueError, match="この機体では使えません"):
            execute_motion_command(command, stage)

        assert stage.commands == []


class TestLabels:
    def test_position_of_homed_axes_has_three_decimals(self):
        status = _status(Point3d(12.5, 3.0, -6.25), "xyz")

        assert motion_position_label(status) == "X12.500 Y3.000 Z-6.250"

    def test_unhomed_axes_show_no_position(self):
        status = _status(Point3d(12.5, 3.0, -6.25), "z")

        assert motion_position_label(status) == "X? Y? Z-6.250"

    @pytest.mark.parametrize(
        ("homed", "expected"), [("", ""), ("zx", "xz"), ("yzx", "xyz")]
    )
    def test_homed_axes_are_in_xyz_order(self, homed: str, expected: str):
        status = _status(Point3d(0.0, 0.0, 0.0), homed)

        assert motion_homed_axes(status) == expected
