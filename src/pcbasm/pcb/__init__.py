"""PCB情報抽出モジュール: KiCadからパッド・部品・銅箔情報を抽出."""

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
    build_pad_hierarchy,
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
    "build_pad_hierarchy",
    "pad_id_from_ref",
]
