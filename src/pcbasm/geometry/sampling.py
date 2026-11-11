"""ポリゴン領域からの probe 点サンプリング."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import attrs
import numpy as np
import numpy.typing as npt
import shapely
from shapely import get_parts
from shapely.geometry import MultiPoint, Point as ShapelyPoint, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import polylabel

from pcbasm.geometry.transform import Point2d

_BOUNDS_EPS = 1e-9
_EDGE_INSET_RATIO = 0.5
_ANCHOR_CLEARANCE_RATIO = 1.5
_DEDUP_DIGITS = 9


def sample_points_in_polygons(
    polygons: Iterable[Polygon],
    *,
    min_radius: float,
    min_samples: int,
    max_samples: int,
    outline: Polygon | None = None,
    outline_margin: float = 0.0,
) -> list[Point2d]:
    """ポリゴン領域の内部から probe 用の点を安全かつ広く分散するようサンプルする.

    座標と長さは ``polygons`` と同じ座標系・単位で扱う（呼び出し元では基板座標 [mm]）。

    候補点は、各ポリゴンを min_radius だけ内側にオフセットした領域から作る。

    候補はグリッド点、各連結部分の中心 (pole of inaccessibility)、内側領域の境界寄りの点。

    outline_margin が正なら、outline をその距離だけ内側にオフセットした領域の中に候補を限る。

    outline を渡すと、基板外形 bbox の四隅・辺中央・中心に近い候補を先に選ぶ。

    残りは、境界から遠い点を優先する Farthest Point Sampling で選ぶ。

    Args:
        polygons: 点を置いてよいポリゴン群（例: 銅箔島。呼び出し側でフィルタ済み）
        min_radius: 点がポリゴン境界（穴を含む）から離れるべき最小距離
        min_samples: 最小サンプル数
        max_samples: 最大サンプル数
        outline: 基板外形ポリゴン。指定時は外周側のカバレッジを優先する
        outline_margin: 点が outline 境界から離れるべき最小距離。正の値には outline が必須

    Returns:
        選んだ probe 点のリスト（点数は min_samples 以上 max_samples 以下）

    Raises:
        ValueError: outline_margin が不正（負・outline 無し・内側が空）な場合
        ValueError: 候補点が min_samples に満たない場合
    """
    sampling_outline = _inset_outline(outline, outline_margin)
    allowed_region = sampling_outline if outline_margin > 0.0 else None
    candidates, clearance = _collect_candidates(
        polygons,
        min_radius,
        allowed_region=allowed_region,
    )

    if len(candidates) < min_samples:
        raise ValueError(
            "probe点の候補数が min_samples に満たない: "
            f"候補数={len(candidates)}, min_samples={min_samples}, "
            f"min_radius={min_radius}, outline_margin={outline_margin}"
        )

    target = min(max_samples, len(candidates))
    clearance_weight = np.minimum(clearance / (2.0 * min_radius), 1.0)
    selected = _coverage_fps_indices(
        candidates,
        clearance,
        clearance_weight,
        target_count=target,
        min_radius=min_radius,
        outline=sampling_outline,
    )
    return [Point2d(x=float(x), y=float(y)) for x, y in candidates[selected]]


def _inset_outline(
    outline: Polygon | None, outline_margin: float
) -> BaseGeometry | None:
    """Sampling に使う内側 outline を返す."""
    if outline_margin < 0.0:
        raise ValueError(
            f"outline_marginは0以上である必要があります。outline_margin={outline_margin}"
        )
    if outline_margin == 0.0:
        return outline
    if outline is None:
        raise ValueError("outline_marginを指定する場合はoutlineが必要です")

    inset = outline.buffer(-outline_margin)
    if inset.is_empty:
        raise ValueError(
            "outline_marginにより基板内のprobe可能領域が空になりました。"
            f"outline_margin={outline_margin}"
        )
    return inset


@attrs.frozen
class SamplingDiagnostics:
    """Probe 計画点の安全余裕と基板カバレッジ.

    Attributes:
        point_count: 計画点数
        min_clearance: 点が乗るポリゴン境界までの最小距離 [mm]
        hull_area_ratio: 計画点凸包の面積 / outline 面積
    """

    point_count: int
    min_clearance: float
    hull_area_ratio: float


def sampling_diagnostics(
    points: Sequence[Point2d],
    polygons: Sequence[Polygon],
    outline: Polygon,
) -> SamplingDiagnostics:
    """計画 probe 点の安全余裕と基板カバレッジ指標を計算する.

    ``points`` は :func:`sample_points_in_polygons` の結果（1 点以上、どれかのポリゴンに乗る）とする。

    Args:
        points: 計画 probe 点
        polygons: 点が乗る対象ポリゴン群（例: 銅箔島）
        outline: 基板外形ポリゴン
    """
    clearances = [_clearance_to_polygons(p, polygons) for p in points]
    hull = MultiPoint([(p.x, p.y) for p in points]).convex_hull
    return SamplingDiagnostics(
        point_count=len(points),
        min_clearance=min(clearances),
        hull_area_ratio=hull.area / outline.area,
    )


def _clearance_to_polygons(point: Point2d, polygons: Sequence[Polygon]) -> float:
    """点が乗っているポリゴン境界までの最小距離を返す."""
    shapely_point = ShapelyPoint(point.x, point.y)
    return min(
        p.boundary.distance(shapely_point) for p in polygons if p.covers(shapely_point)
    )


def _collect_candidates(
    polygons: Iterable[Polygon],
    min_radius: float,
    *,
    allowed_region: BaseGeometry | None = None,
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """min_radius 内側にオフセットした領域の候補点と、各点のクリアランスを収集する.

    各 polygon を min_radius 内側にオフセットし、min_radius/2 ステップのグリッド点、
    各連結部分の中心 (pole of inaccessibility)、内側領域の境界寄り点を候補に含める。
    クリアランスは元の ``polygon.boundary`` (穴を含む) からの距離で、銅箔境界からの
    安全余裕を表す。全候補は ``buffer(-min_radius)`` の内部または境界上にあるため
    最小クリアランス min_radius を満たす。
    """
    step = min_radius / 2.0
    edge_inset = max(min_radius * _EDGE_INSET_RATIO, _BOUNDS_EPS)
    points: list[npt.NDArray[np.float64]] = []
    clearances: list[npt.NDArray[np.float64]] = []

    for polygon in polygons:
        inner = polygon.buffer(-min_radius)
        if allowed_region is not None:
            inner = inner.intersection(allowed_region)
        if inner.is_empty:
            continue

        minx, miny, maxx, maxy = inner.bounds
        xs = np.arange(minx, maxx + _BOUNDS_EPS, step)
        ys = np.arange(miny, maxy + _BOUNDS_EPS, step)
        grid_x, grid_y = (g.ravel() for g in np.meshgrid(xs, ys, indexing="ij"))
        inside = shapely.contains_xy(inner, grid_x, grid_y)
        polygon_points = [np.column_stack((grid_x[inside], grid_y[inside]))]

        # 各連結部分の中心を確実に候補へ含める (小島でグリッドが中心を逃すのを防ぐ)
        for part in get_parts(inner):
            if not isinstance(part, Polygon):
                continue
            center = polylabel(part, tolerance=min_radius / 10.0)
            polygon_points.append(np.array([[center.x, center.y]], dtype=np.float64))
            polygon_points.append(_boundary_nearby_points(part, step, edge_inset))

        coords = np.concatenate(polygon_points)
        points.append(coords)
        clearances.append(
            np.asarray(
                shapely.distance(polygon.boundary, shapely.points(coords)),
                dtype=np.float64,
            )
        )

    if not points:
        return (
            np.empty((0, 2), dtype=np.float64),
            np.empty((0,), dtype=np.float64),
        )
    return _deduplicate_candidates(np.concatenate(points), np.concatenate(clearances))


def _boundary_nearby_points(
    polygon: Polygon, step: float, edge_inset: float
) -> npt.NDArray[np.float64]:
    """内側オフセット済み polygon の境界近傍から候補点を作る."""
    rings = [
        ring
        for part in get_parts(polygon.buffer(-edge_inset))
        if isinstance(part, Polygon)
        for ring in (part.exterior, *part.interiors)
        if ring.length > 0.0
    ]
    if not rings:
        return np.empty((0, 2), dtype=np.float64)
    return np.concatenate(
        [
            shapely.get_coordinates(
                shapely.line_interpolate_point(ring, np.arange(0.0, ring.length, step))
            )
            for ring in rings
        ]
    )


def _deduplicate_candidates(
    points: npt.NDArray[np.float64], clearances: npt.NDArray[np.float64]
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """同一点候補をまとめ、同じ座標では大きい clearance を採用する.

    まとめた点は最初に現れた順に並べ、clearance が同じなら先に現れた点を残す。
    """
    _, first_index, inverse = np.unique(
        _round_like_builtin(points, _DEDUP_DIGITS),
        axis=0,
        return_index=True,
        return_inverse=True,
    )
    groups = np.argsort(np.argsort(first_index))[inverse.ravel()]
    order = np.lexsort((np.arange(len(groups)), -clearances, groups))
    is_first = np.ones(len(order), dtype=bool)
    is_first[1:] = groups[order[1:]] != groups[order[:-1]]
    chosen = order[is_first]
    return points[chosen], clearances[chosen]


def _round_like_builtin(
    values: npt.NDArray[np.float64], digits: int
) -> npt.NDArray[np.float64]:
    """組み込みの ``round(v, digits)`` と同じ値を要素ごとに返す.

    ``np.round`` は ``10**digits`` 倍したときの丸め誤差で、端数が半分付近の値だけ組み込みと食い違う。
    倍した値の端数が 0.5 から 1 ulp 以内の値だけ、組み込みの ``round`` で計算し直す。
    """
    rounded = np.round(values, digits)
    scaled = values * 10.0**digits
    near_half = np.abs(scaled - np.floor(scaled) - 0.5) <= np.spacing(np.abs(scaled))
    rounded[near_half] = [round(v, digits) for v in values[near_half].tolist()]
    return rounded


def _coverage_fps_indices(
    candidates: npt.NDArray[np.float64],
    clearance: npt.NDArray[np.float64],
    clearance_weight: npt.NDArray[np.float64],
    *,
    target_count: int,
    min_radius: float,
    outline: BaseGeometry | None,
) -> list[int]:
    """Anchor と clearance 飽和付き FPS でインデックスを選ぶ."""
    if target_count <= 0:
        return []

    selected: list[int] = []

    if outline is not None and not outline.is_empty:
        preferred_clearance = min_radius * _ANCHOR_CLEARANCE_RATIO
        for anchor in _outline_anchor_points(outline):
            idx = _nearest_unselected_index(
                candidates,
                anchor,
                selected,
                clearance,
                preferred_clearance,
            )
            if idx is None:
                break
            selected.append(idx)
            if len(selected) >= target_count:
                return selected

    if not selected:
        selected.append(int(clearance_weight.argmax()))

    min_dist = _distance_to_selected(candidates, selected)

    while len(selected) < target_count:
        next_idx = int((min_dist * clearance_weight).argmax())
        if min_dist[next_idx] == 0.0:
            break
        selected.append(next_idx)
        new_dist = np.linalg.norm(candidates - candidates[next_idx], axis=1)
        min_dist = np.minimum(min_dist, new_dist)

    return selected


def _outline_anchor_points(outline: BaseGeometry) -> npt.NDArray[np.float64]:
    """Outline bbox の四隅・辺中央・中心を anchor として返す."""
    minx, miny, maxx, maxy = outline.bounds
    cx = (minx + maxx) / 2.0
    cy = (miny + maxy) / 2.0
    return np.array(
        [
            (minx, miny),
            (maxx, miny),
            (minx, maxy),
            (maxx, maxy),
            (cx, miny),
            (minx, cy),
            (maxx, cy),
            (cx, maxy),
            (cx, cy),
        ],
        dtype=np.float64,
    )


def _nearest_unselected_index(
    candidates: npt.NDArray[np.float64],
    anchor: npt.NDArray[np.float64],
    selected: list[int],
    clearance: npt.NDArray[np.float64],
    preferred_clearance: float,
) -> int | None:
    """Anchor に最も近い未選択候補の index を返す."""
    if len(selected) >= len(candidates):
        return None

    distances = np.linalg.norm(candidates - anchor, axis=1)
    if selected:
        distances[np.array(selected, dtype=np.int64)] = np.inf

    preferred = distances.copy()
    preferred[clearance < preferred_clearance] = np.inf
    if np.isfinite(preferred).any():
        return int(preferred.argmin())
    return int(distances.argmin())


def _distance_to_selected(
    candidates: npt.NDArray[np.float64], selected: list[int]
) -> npt.NDArray[np.float64]:
    """各候補から既選択集合への最短距離を返す."""
    distances = np.full(len(candidates), np.inf, dtype=np.float64)
    for idx in selected:
        distances = np.minimum(
            distances,
            np.linalg.norm(candidates - candidates[idx], axis=1),
        )
    return distances
