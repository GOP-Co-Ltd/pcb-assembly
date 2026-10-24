"""基板データの CSV / JSON 書き出し（extract_pcb の成果物）."""

import csv
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from cattrs.preconf.json import make_converter
from shapely import Polygon

from .model import Component, Copper, Layer, Outline, Pad

PNP_CSV_HEADER: tuple[str, ...] = (
    "Designator",
    "Value",
    "Package",
    "X",
    "Y",
    "Rotation",
    "Layer",
)

_converter = make_converter()
_converter.register_unstructure_hook(Layer, lambda v: v.value)
_converter.register_unstructure_hook(
    Polygon,
    lambda p: {
        "exterior": list(p.exterior.coords),
        "holes": [list(interior.coords) for interior in p.interiors],
    },
)


def save_outline(outline: Outline, path: Path) -> None:
    """外形を JSON で保存する（外周だけを保存し、穴は保存しない）."""
    _write_json(path, {"polygon": list(outline.polygon.exterior.coords)})


def save_pnp_csv(components: Iterable[Component], path: Path) -> None:
    """部品を Pick and Place CSV で保存する."""
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(PNP_CSV_HEADER)
        for comp in components:
            writer.writerow(
                [
                    comp.designator,
                    comp.value,
                    comp.package,
                    f"{comp.position.x:.4f}",
                    f"{comp.position.y:.4f}",
                    f"{comp.rotation:.2f}",
                    comp.layer.value,
                ]
            )


def save_pads(pads: Iterable[Pad], path: Path) -> None:
    """パッドを JSON で保存する（ポリゴンは穴も保存する）."""
    _write_json(path, [_converter.unstructure(pad) for pad in pads])


def save_copper(coppers: Iterable[Copper], path: Path) -> None:
    """銅箔島を JSON で保存する（ポリゴンは穴も保存する）."""
    _write_json(path, [_converter.unstructure(copper) for copper in coppers])


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
