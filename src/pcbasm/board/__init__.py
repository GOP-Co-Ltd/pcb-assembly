"""Pcbnew に依存しない基板データ.

座標はすべて「基板座標」で、単位は mm。

基板座標の原点は基板外形 bbox の左上（KiCad 座標の最小 X・最小 Y）。

X は右向き、Y は下向き（KiCad と同じ）。

KiCad ファイルからの読込は :mod:`pcbasm.kicad`、書き出しは :mod:`.export`、描画は :mod:`.plot` に置く。
"""

from .model import Board, Component, Copper, Layer, Outline, Pad

__all__ = [
    "Board",
    "Component",
    "Copper",
    "Layer",
    "Outline",
    "Pad",
]
