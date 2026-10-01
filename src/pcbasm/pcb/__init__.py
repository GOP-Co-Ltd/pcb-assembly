"""KiCad からパッド・部品・銅箔情報を抽出する PCB 情報抽出モジュール.

座標はすべて「基板座標」で、単位は mm。

基板座標の原点は基板外形 bbox の左上（KiCad 座標の最小 X・最小 Y）。

X は右向き、Y は下向き（KiCad と同じ）。

pcbnew（KiCad 9 以降）が無い環境では、このパッケージのどの名前も import できない。
``Pad`` や ``Component`` だけを使う場合でも、``__init__`` が ``PcbFile``
（pcbnew を import する）を読み込むため。

``units`` / ``footprint`` / ``generate`` は re-export しないので、モジュールを
直接 import する。
"""

from .board import (
    PNP_CSV_HEADER,
    Component,
    ComponentList,
    Copper,
    CopperList,
    Layer,
    Outline,
    Pad,
    PadList,
)
from .grouping import (
    HierKey,
    PadHierarchy,
    PadHierarchyNode,
    PadRef,
    PadShapeKey,
    pad_id_from_ref,
)
from .kicad import PcbFile

__all__ = [
    "Component",
    "ComponentList",
    "Copper",
    "CopperList",
    "HierKey",
    "Layer",
    "Outline",
    "Pad",
    "PadHierarchy",
    "PadHierarchyNode",
    "PadList",
    "PadRef",
    "PadShapeKey",
    "PNP_CSV_HEADER",
    "PcbFile",
    "pad_id_from_ref",
]
