"""ポリゴンの結合・分解・変換・外接矩形ユーティリティ."""

from __future__ import annotations

from collections.abc import Sequence

import attrs
import shapely.ops
from shapely import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Polygon,
)
from shapely.coords import CoordinateSequence
from shapely.geometry.base import BaseGeometry

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


def exterior_points(polygon: Polygon) -> list[Point2d]:
    """``polygon.exterior.coords`` を ``Point2d`` リストとして返す（閉環、末尾は始点の重複）.

    内側ホール（``polygon.interiors``）は含めない。
    """
    return [Point2d(x=x, y=y) for x, y in polygon.exterior.coords]


def display_rings(
    polygon: Polygon, *, tolerance: float = 0.0, precision: int = 3
) -> tuple[tuple[tuple[float, float], ...], ...]:
    """Polygon を表示用の環列（exterior が先、以降が穴）へ落とす.

    WebUI の SVG など、座標そのものではなく形を見せる用途向け。``tolerance``
    を与えると Douglas-Peucker で頂点を間引き、``precision`` 桁へ丸めて
    転送量を抑える。簡略化で潰れた島は空の環列を返すので、呼び出し側は
    空を扱えるようにする。

    Args:
        polygon: 対象ポリゴン
        tolerance: 簡略化の許容誤差 [mm]（0 なら簡略化しない）
        precision: 座標の小数桁

    Returns:
        環ごとの座標列。各環は閉環（末尾が始点の重複）。
    """
    shape = polygon if tolerance <= 0 else polygon.simplify(tolerance)
    if not isinstance(shape, Polygon) or shape.is_empty:
        return ()
    return tuple(
        tuple((round(x, precision), round(y, precision)) for x, y in ring.coords)
        for ring in (shape.exterior, *shape.interiors)
    )


def offset_components(polygon: Polygon, depth: float) -> list[Polygon]:
    """``polygon.buffer(-depth)`` の結果から ``Polygon`` のみを抽出する.

    MultiPolygon は連結成分に分解する。Point/LineString/GeometryCollection
    内の非Polygon要素は除外する。``depth <= 0`` なら ``polygon`` 自身を返す。
    空・不正な結果は空リストとなる。
    """
    offset = polygon.buffer(-depth) if depth > 0 else polygon

    if offset.is_empty:
        return []

    if isinstance(offset, Polygon):
        return [offset] if offset.is_valid else []

    if isinstance(offset, (MultiPolygon, GeometryCollection)):
        geoms: list[BaseGeometry] = list(offset.geoms)
        return [g for g in geoms if isinstance(g, Polygon) and not g.is_empty]

    return []


@attrs.frozen
class OrientedBox:
    """最小回転外接矩形.

    Attributes:
        corner: 基準頂点（``minimum_rotated_rectangle`` の先頭頂点）
        edge_a: ``corner`` から次の頂点へのベクトル
        edge_b: ``corner`` から前の頂点へのベクトル
    """

    corner: Point2d
    edge_a: Point2d
    edge_b: Point2d

    @property
    def long_edge(self) -> Point2d:
        """長辺ベクトル（同長なら ``edge_a``）."""
        return self.edge_a if self.edge_a.norm >= self.edge_b.norm else self.edge_b

    @property
    def short_edge(self) -> Point2d:
        """短辺ベクトル（同長なら ``edge_b``）."""
        return self.edge_b if self.edge_a.norm >= self.edge_b.norm else self.edge_a

    @property
    def long_length(self) -> float:
        return self.long_edge.norm

    @property
    def short_length(self) -> float:
        return self.short_edge.norm

    def center_line(self) -> tuple[Point2d, Point2d]:
        """長辺方向に矩形を貫く中央線の (始点, 終点) を返す.

        始点・終点は短辺の中点。``edge_b`` が ``edge_a`` 以上の長さなら
        ``edge_a`` 側の中点から ``edge_b`` 方向へ、そうでなければ逆向きに辿る。
        """
        half_a = self.edge_a * 0.5
        half_b = self.edge_b * 0.5
        if self.edge_b.norm >= self.edge_a.norm:
            return (self.corner + half_a, self.corner + self.edge_b + half_a)
        return (self.corner + self.edge_a + half_b, self.corner + half_b)


def oriented_bbox(polygon: Polygon) -> OrientedBox | None:
    """最小回転外接矩形を返す。退化していれば ``None``."""
    mrr = polygon.minimum_rotated_rectangle
    if not isinstance(mrr, Polygon) or mrr.is_empty:
        return None
    coords = list(mrr.exterior.coords)
    if len(coords) < 5:
        return None
    corner = Point2d(coords[0][0], coords[0][1])
    return OrientedBox(
        corner=corner,
        edge_a=Point2d(coords[1][0], coords[1][1]) - corner,
        edge_b=Point2d(coords[3][0], coords[3][1]) - corner,
    )


def clip_segment(
    polygon: Polygon, start: Point2d, end: Point2d
) -> list[tuple[Point2d, Point2d]]:
    """線分 ``start``→``end`` とポリゴンの交線区間を進行方向順に返す.

    各区間は進行方向に沿って ``(手前, 奥)`` の順。交差が無ければ空。
    """
    direction = end - start
    length = direction.norm
    if length <= 0:
        return []
    u = direction * (1.0 / length)

    inter = LineString([(start.x, start.y), (end.x, end.y)]).intersection(polygon)
    if inter.is_empty:
        return []

    if isinstance(inter, LineString):
        lines: list[LineString] = [inter]
    elif isinstance(inter, MultiLineString):
        lines = [g for g in inter.geoms if isinstance(g, LineString)]
    else:
        return []

    def along(p: Point2d) -> float:
        return (p - start).x * u.x + (p - start).y * u.y

    intervals: list[tuple[Point2d, Point2d]] = []
    for line in lines:
        pts = [Point2d(x, y) for x, y in line.coords]
        if len(pts) < 2:
            continue
        pts.sort(key=along)
        intervals.append((pts[0], pts[-1]))

    intervals.sort(key=lambda seg: along(seg[0]))
    return intervals
