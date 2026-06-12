"""ポリゴンの結合・分解・変換ユーティリティ."""

from __future__ import annotations

from collections.abc import Sequence

import shapely.ops
from shapely import MultiPolygon, Polygon
from shapely.coords import CoordinateSequence

from .transform import Point2d, Transform


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


def transform_polygon(polygon: Polygon, transform: Transform) -> Polygon:
    """ポリゴンの全頂点に2D Transformを適用したPolygonを返す.

    exteriorと各interior（穴）の頂点を ``transform.apply`` で写して
    再構築する。

    Args:
        polygon: 入力ポリゴン
        transform: 適用する2D Transform

    Returns:
        全頂点を変換したPolygon
    """

    def ring(coords: CoordinateSequence) -> list[tuple[float, float]]:
        points = [
            transform.apply(Point2d(x=float(c[0]), y=float(c[1]))) for c in coords
        ]
        return [(p.x, p.y) for p in points]

    return Polygon(
        ring(polygon.exterior.coords),
        holes=[ring(interior.coords) for interior in polygon.interiors],
    )
