from __future__ import annotations

from collections.abc import Iterable
from typing import Self, override


class GCode:
    """G-codeコマンドを管理するクラス."""

    _buffer: list[str]

    def __init__(self, gcode: GCodeLike | None = None) -> None:
        """GCodeオブジェクトを初期化する.

        Args:
            gcode: G-codeコマンド（文字列、Iterable、または別のGCodeオブジェクト）
        """
        match gcode:
            case None:
                self._buffer = []
            case str():
                self._buffer = [gcode]
            case GCode():
                self._buffer = gcode.to_list()
            case _:
                self._buffer = list(gcode)

    @override
    def __str__(self) -> str:
        """G-codeコマンドを改行区切りの文字列として返す."""
        return "\n".join(self._buffer)

    @override
    def __repr__(self) -> str:
        return f"GCode({str(self)})"

    @override
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, GCode):
            return NotImplemented
        return str(self) == str(other)

    @override
    def __hash__(self) -> int:
        return hash(str(self))

    def __add__(self, other: GCodeLike) -> Self:
        result = self.copy()
        result.append(other)
        return result

    def copy(self) -> Self:
        return self.__class__(self)

    def to_list(self) -> list[str]:
        """内部バッファのコピーをリストとして返す."""
        return self._buffer.copy()

    def append(self, gcode: GCodeLike) -> None:
        """G-codeコマンドを追加する.

        Args:
            gcode: 追加するG-codeコマンド
        """
        self._buffer.extend(GCode(gcode).to_list())


type GCodeLike = str | Iterable[str] | GCode

PRESENT_MACRO = "PRESENT"


def homing(x: bool = False, y: bool = False, z: bool = False) -> GCode:
    """ホーミングコマンドを生成する.

    Args:
        x: X軸をホーミングするか
        y: Y軸をホーミングするか
        z: Z軸をホーミングするか

    Returns:
        ホーミングのGCode。引数がすべてFalseの場合は全軸ホーミング
    """
    if not (x or y or z):
        return GCode("G28")
    axes = []
    if x:
        axes.append("X")
    if y:
        axes.append("Y")
    if z:
        axes.append("Z")
    return GCode(f"G28 {' '.join(axes)}")


def move(
    x: float | None = None,
    y: float | None = None,
    z: float | None = None,
    velocity: float | None = None,
) -> GCode:
    """移動コマンドを生成する.

    Args:
        x: X座標 [mm]
        y: Y座標 [mm]
        z: Z座標 [mm]
        velocity: 移動速度 [mm/s]

    Returns:
        移動のGCode。すべてNoneの場合は空のGCode
    """
    parts = []
    if x is not None:
        parts.append(f"X{x}")
    if y is not None:
        parts.append(f"Y{y}")
    if z is not None:
        parts.append(f"Z{z}")
    if velocity is not None:
        parts.append(f"F{velocity * 60}")
    if not parts:
        return GCode()
    return GCode(f"G1 {' '.join(parts)}")


def wait(seconds: float) -> GCode:
    """指定秒数待機するコマンドを生成する.

    Args:
        seconds: 待機時間 [秒]

    Returns:
        待機のGCode。0秒の場合は空のGCode
    """
    if seconds <= 0:
        return GCode()
    return GCode(f"G4 P{int(seconds * 1000)}")


def wait_for_done() -> GCode:
    """すべての動作完了を待つコマンドを生成する."""
    return GCode("M400")


def present() -> GCode:
    """基板を差し出すPRESENTマクロを実行するコマンドを生成する."""
    return GCode(PRESENT_MACRO)


def firmware_restart() -> GCode:
    """Klipper のファームウェア再起動コマンドを生成する."""
    return GCode("FIRMWARE_RESTART")


def relax() -> GCode:
    """モーターを脱力するコマンドを生成する."""
    return GCode("M84")
