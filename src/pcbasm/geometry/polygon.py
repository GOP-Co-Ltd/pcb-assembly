"""ポリゴンの結合・分解ユーティリティ."""

from __future__ import annotations

from collections.abc import Sequence

import shapely.ops
from shapely import MultiPolygon, Polygon


def merge_islands(polygons: Sequence[Polygon], snap_mm: float) -> list[Polygon]:
    """ポリゴン群を融合し、連結成分（島）ごとのPolygonに分解して返す.

    unary_union で融合した後、closing（``buffer(+snap_mm)`` → ``buffer(-snap_mm)``）
    で snap_mm 未満のヘアライン状の隙間を橋渡しし、本来連結している領域が
    別の島に分かれるのを防ぐ。

    Args:
        polygons: 入力ポリゴン群
        snap_mm: 橋渡しする隙間幅 [mm]。これ未満の隙間は同一島とみなす

    Returns:
        連結成分ごとの空でない Polygon のリスト。空入力なら空リスト。
    """
    if not polygons:
        return []

    merged = shapely.ops.unary_union(polygons)
    closed = merged.buffer(snap_mm).buffer(-snap_mm)
    islands = closed.geoms if isinstance(closed, MultiPolygon) else [closed]
    return [
        island
        for island in islands
        if isinstance(island, Polygon) and not island.is_empty
    ]
