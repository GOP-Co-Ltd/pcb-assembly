"""新ファームの機体（``[pnp.motion]``）の操作パネルの命令と、位置と荷重の表示文字列.

命令の型は Klipper の機体と同じ :data:`~pcbasm.machine.commands.MachineCommand` を使い、
G-code にせず :class:`~pcbasm.hal.motion.MotionStage` で実行する。
Klipper に依存する命令（焦点 Z・キャップ位置・任意の G-code）は受け付けない。
速さは指定せず、ステージの既定（各軸の ``max_velocity`` を超えない最大の速さ）で動かす。
Klipper の機体の操作パネルが printer.cfg の ``max_velocity`` で動かすのに合わせる。
"""

from __future__ import annotations

from pcbasm.hal import MotionStage, MotionStatus
from pcbasm.machine.commands import (
    FocusZ,
    Home,
    Jog,
    MachineCommand,
    Move,
    MoveToCap,
    RawGCode,
    Relax,
    Tare,
)

_AXES = ("x", "y", "z")


def execute_motion_command(command: MachineCommand, stage: MotionStage) -> None:
    """命令をステージで実行し、終わるまで待つ.

    原点出しと移動は、途中でジョブを中止しても止まらない（止めるのは緊急停止だけ）。

    Raises:
        ValueError: 可動域の外、原点出しをしていない軸、ロードセルが無いか値が来ていない（Tare）、
            またはこの機体で使えない命令の場合
        MotionError: ステージが shutdown している、または話せない場合
    """
    match command:
        case Home(axes=axes):
            stage.home(axes)
        case Jog(axis=axis, distance=distance):
            status = stage.status()
            if axis not in status.homed:
                raise ValueError(f"原点出しをしていない軸はジョグできません: {axis}")
            target = getattr(status.position, axis) + distance
            stage.move_to(
                x=target if axis == "x" else None,
                y=target if axis == "y" else None,
                z=target if axis == "z" else None,
            )
        case Move(x=x, y=y, z=z):
            stage.move_to(x=x, y=y, z=z)
        case Relax():
            stage.relax()
        case Tare():
            stage.tare()
        case FocusZ() | MoveToCap() | RawGCode():
            raise ValueError(
                f"{type(command).__name__} はこの機体では使えません（Klipper の機体だけの操作です）"
            )


def motion_position_label(status: MotionStatus) -> str:
    """位置を小数 3 桁で書く。原点出しをしていない軸は ``?`` にする（位置が意味を持たないため）."""
    return " ".join(
        f"{axis.upper()}{getattr(status.position, axis):.3f}"
        if axis in status.homed
        else f"{axis.upper()}?"
        for axis in _AXES
    )


def motion_homed_axes(status: MotionStatus) -> str:
    """原点出しを済ませた軸を ``xyz`` の順に並べる（Klipper の ``toolhead.homed_axes`` と同じ書き方）."""
    return "".join(axis for axis in _AXES if axis in status.homed)


def motion_load_label(status: MotionStatus) -> str | None:
    """荷重を小数 1 桁で書く（``荷重 12.3 g``）。読めなければ理由を書き、ロードセルが無いか読み始めなら None."""
    if status.load_error is not None:
        return f"ロードセル: {status.load_error}"
    if status.load is None:
        return None
    # -0.04 を「-0.0」と書かない
    return f"荷重 {round(status.load, 1) + 0.0:.1f} g"
