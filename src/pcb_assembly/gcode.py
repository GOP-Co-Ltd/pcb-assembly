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
