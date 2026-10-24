"""マシン操作パネルの命令と、その G-code.

REST の /api/machine/command と対話ジョブの command が共用する。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

import attrs

from pcbasm.config import MachineConfig
from pcbasm.gcode import GCode
from pcbasm.hal import XYZStage
from pcbasm.vision import load_focus_z

# 1 回の送信の timeout [s]。M400 で到達を待つので長め
COMMAND_TIMEOUT = 60.0

type Axis = Literal["x", "y", "z"]


@attrs.frozen
class Home:
    """指定した軸をホーミングする（空なら全軸）."""

    axes: tuple[Axis, ...] = ()


@attrs.frozen
class Jog:
    """1 軸を現在位置から ``distance`` [mm] 動かす."""

    axis: Axis
    distance: float


@attrs.frozen
class Move:
    """指定した軸を絶対座標 [mm] へ動かす."""

    x: float | None = None
    y: float | None = None
    z: float | None = None


@attrs.frozen
class Relax:
    """モーターを脱力する."""


@attrs.frozen
class FocusZ:
    """カメラ校正の焦点 Z へ動かす."""


@attrs.frozen
class MoveToCap:
    """ノズルキャップ位置へ動かす（脱力はしない）."""


@attrs.frozen
class RawGCode:
    """任意の G-code を送る."""

    text: str


type MachineCommand = Home | Jog | Move | Relax | FocusZ | MoveToCap | RawGCode


def to_gcode(
    command: MachineCommand, stage: XYZStage, machine: Callable[[], MachineConfig]
) -> GCode:
    """命令を送る G-code にする（relax 以外は M400 で到達を待つ）.

    設定は焦点 Z とキャップ位置のときだけ読む。設定エラーでもホーミング・ジョグ・脱力はできる。

    Raises:
        ValueError: 可動域外、焦点 Z が無い、キャップ位置が未記録の場合
    """
    match command:
        case Home(axes=axes):
            commands = GCode.homing(x="x" in axes, y="y" in axes, z="z" in axes)
        case Jog(axis=axis, distance=distance):
            commands = stage.move(
                x=distance if axis == "x" else None,
                y=distance if axis == "y" else None,
                z=distance if axis == "z" else None,
                relative=True,
            )
        case Move(x=x, y=y, z=z):
            commands = stage.move(x=x, y=y, z=z)
        case Relax():
            return GCode.relax()
        case FocusZ():
            z = load_focus_z(machine().camera.calibration_file)
            if z is None:
                raise ValueError(
                    "フォーカスZが取得できません（カメラキャリブレーション未設定）"
                )
            commands = stage.move(z=z)
        case MoveToCap():
            cap = machine().paste_dispenser.nozzle_cap.position
            if cap is None:
                raise ValueError("ノズルキャップ位置が未記録です")
            commands = stage.travel_to(cap)
        case RawGCode(text=text):
            commands = GCode(text)
    return commands + GCode.wait_for_done()
