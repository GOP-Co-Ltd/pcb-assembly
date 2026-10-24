"""マシン操作パネルの命令を、REST と対話ジョブの command の共通の契約から pcbasm の命令へ変換する."""

from __future__ import annotations

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
from web.contract.models import (
    FocusZCommand,
    GCodeCommand,
    HomeCommand,
    JogCommand,
    MachineCommandRequest,
    MoveCommand,
    MoveToCapCommand,
    RelaxCommand,
)


def to_command(request: MachineCommandRequest) -> MachineCommand:
    """検証済みの命令を pcbasm の命令にする."""
    match request.root:
        case HomeCommand(axes=axes):
            return Home(tuple(axes))
        case JogCommand(axis=axis, distance=distance):
            return Jog(axis, distance)
        case MoveCommand(x=x, y=y, z=z):
            return Move(x, y, z)
        case RelaxCommand():
            return Relax()
        case FocusZCommand():
            return FocusZ()
        case MoveToCapCommand():
            return MoveToCap()
        case GCodeCommand(gcode=gcode):
            return RawGCode(gcode)
