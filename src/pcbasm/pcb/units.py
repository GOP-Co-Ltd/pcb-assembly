"""KiCad 内部単位（nm、signed 32-bit）と mm の変換.

pcbnew に依存するため ``pcbasm.pcb`` からは re-export しない（``generate`` と同じ扱い）。
"""

from __future__ import annotations

from typing import cast

import pcbnew

KICAD_COORD_MIN_NM = -(2**31)
KICAD_COORD_MAX_NM = 2**31 - 1
KICAD_MIN_NONZERO_MM = 1 / 1_000_000
KICAD_MAX_COORD_MM = KICAD_COORD_MAX_NM / 1_000_000


class KicadError(RuntimeError):
    """KiCad 環境・座標に起因するエラーの基底."""


class KicadCoordinateError(KicadError):
    """KiCad の signed 32-bit 内部座標で表現できない値."""


def from_mm(value: float) -> int:
    """Mm を内部単位（nm）に変換する."""
    return cast(int, pcbnew.FromMM(value))


def to_mm(value: int | float) -> float:
    """内部単位（nm）を mm に変換する."""
    return float(value) / 1_000_000.0


def is_kicad_length(
    value: int | float, *, maximum_mm: float = KICAD_MAX_COORD_MM
) -> bool:
    """KiCad で非ゼロになり ``VECTOR2I`` に収まる長さ [mm] か."""
    return KICAD_MIN_NONZERO_MM <= value <= maximum_mm


def vector(x: float, y: float) -> pcbnew.VECTOR2I:
    """Mm 座標を ``VECTOR2I`` に変換する.

    Raises:
        KicadCoordinateError: 座標が内部単位の範囲を超える場合
    """
    coordinates = (from_mm(x), from_mm(y))
    if any(
        value < KICAD_COORD_MIN_NM or value > KICAD_COORD_MAX_NM
        for value in coordinates
    ):
        raise KicadCoordinateError(
            "footprint形状から算出した位置がKiCadの座標範囲を超えています"
        )
    return pcbnew.VECTOR2I(*coordinates)
