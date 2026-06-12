"""ポリゴン領域からのprobe点サンプリング."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from itertools import product

import attrs
import numpy as np
import numpy.typing as npt
from shapely import get_parts
from shapely.geometry import MultiPoint, Point as ShapelyPoint, Polygon
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
) -> list[Point2d]:
    """ポリゴン領域の内部からprobe用の点を安全かつ広く分散するようサンプルする.

    各ポリゴンを min_radius だけ内側にオフセットした領域内に、細かめのグリッド候補、
    各連結部分の中心(pole of inaccessibility)、および内側領域の境界寄り候補を生成する。
    outline が指定された場合は基板外形 bbox の anchor に近い候補を先に取り、残りを
    clearance を飽和させた Farthest Point Sampling で選ぶ。

    Args:
        polygons: 入力ポリゴン群（例: 銅箔島のpolygon。呼び出し側でフィルタ済みを想定）
        min_radius: probe ground対応半径 [mm]
        min_samples: 最小サンプル数 (3以上を想定)
        max_samples: 最大サンプル数
        outline: 基板外形ポリゴン。指定時は外周側のカバレッジを優先する

    Returns:
        選ばれたprobe点 (Point2d) のリスト。

    Raises:
        ValueError: 候補点が min_samples に満たない場合
    """
    candidates, clearance = _collect_candidates(polygons, min_radius)

    if len(candidates) < min_samples:
        raise ValueError(
            "probe点の候補数が min_samples に満たない: "
            f"候補数={len(candidates)}, min_samples={min_samples}, "
            f"min_radius={min_radius}"
        )

    target = min(max_samples, len(candidates))
    clearance_weight = np.minimum(clearance / (2.0 * min_radius), 1.0)
    selected = _coverage_fps_indices(
        candidates,
        clearance,
        clearance_weight,
        target_count=target,
        min_radius=min_radius,
        outline=outline,
    )
    return [Point2d(x=float(x), y=float(y)) for x, y in candidates[selected]]


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
) -> SamplingDiagnostics | None:
    """計画 probe 点の安全余裕と基板カバレッジ指標を計算する.

    Args:
        points: 計画 probe 点
        polygons: 点が乗る対象ポリゴン群（例: 銅箔島）
        outline: 基板外形ポリゴン

    Returns:
        SamplingDiagnostics。points が空の場合は None
    """
    if not points:
        return None
    clearances = [_clearance_to_polygons(p, polygons) for p in points]
    hull = MultiPoint([(p.x, p.y) for p in points]).convex_hull
    outline_area = outline.area
    hull_ratio = hull.area / outline_area if outline_area > 0.0 else 0.0
    return SamplingDiagnostics(
        point_count=len(points),
        min_clearance=min(clearances),
        hull_area_ratio=hull_ratio,
    )


def _clearance_to_polygons(point: Point2d, polygons: Sequence[Polygon]) -> float:
    """点が乗っているポリゴン境界までの最小距離を返す."""
    shapely_point = ShapelyPoint(point.x, point.y)
    containing = [p for p in polygons if p.covers(shapely_point)]
    candidates = containing or list(polygons)
    return min(p.boundary.distance(shapely_point) for p in candidates)


def _collect_candidates(
    polygons: Iterable[Polygon], min_radius: float
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """min_radius内側にオフセットした領域の候補点と、各点のクリアランスを収集する.

    各polygonを min_radius 内側にオフセットし、min_radius/2 ステップのグリッド点、
    各連結部分の中心 (pole of inaccessibility)、内側領域の境界寄り点を候補に含める。
    クリアランスは元の ``polygon.boundary`` (穴を含む) からの距離で、銅箔境界からの
    安全余裕を表す。全候補は ``buffer(-min_radius)`` の内部または境界上にあるため
    最小クリアランス min_radius を満たす。
    """
    step = min_radius / 2.0
    edge_inset = max(min_radius * _EDGE_INSET_RATIO, _BOUNDS_EPS)
    points: list[tuple[float, float]] = []
    clearances: list[float] = []

    for polygon in polygons:
        inner = polygon.buffer(-min_radius)
        if inner.is_empty:
            continue
        boundary = polygon.boundary

        minx, miny, maxx, maxy = inner.bounds
        xs = np.arange(minx, maxx + _BOUNDS_EPS, step)
        ys = np.arange(miny, maxy + _BOUNDS_EPS, step)
        for x, y in product(xs, ys):
            pt = ShapelyPoint(float(x), float(y))
            if inner.contains(pt):
                points.append((pt.x, pt.y))
                clearances.append(boundary.distance(pt))

        # 各連結部分の中心を確実に候補へ含める (小島でグリッドが中心を逃すのを防ぐ)
        for part in get_parts(inner):
            if not isinstance(part, Polygon):
                continue
            center = polylabel(part, tolerance=min_radius / 10.0)
            points.append((center.x, center.y))
            clearances.append(boundary.distance(center))

            for pt in _boundary_nearby_points(part, step, edge_inset):
                points.append((pt.x, pt.y))
                clearances.append(boundary.distance(pt))

    if not points:
        return (
            np.empty((0, 2), dtype=np.float64),
            np.empty((0,), dtype=np.float64),
        )
    return _deduplicate_candidates(points, clearances)


def _boundary_nearby_points(
    polygon: Polygon, step: float, edge_inset: float
) -> list[ShapelyPoint]:
    """内側オフセット済みpolygonの境界近傍から候補点を作る."""
    inset = polygon.buffer(-edge_inset)
    if inset.is_empty:
        return []

    points: list[ShapelyPoint] = []
    for part in get_parts(inset):
        if not isinstance(part, Polygon):
            continue
        rings = [part.exterior, *part.interiors]
        for ring in rings:
            if ring.length <= 0.0:
                continue
            for distance in np.arange(0.0, ring.length, step):
                points.append(ring.interpolate(float(distance)))
    return points


def _deduplicate_candidates(
    points: list[tuple[float, float]], clearances: list[float]
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """同一点候補をまとめ、同じ座標では大きい clearance を採用する."""
    order: list[tuple[float, float]] = []
    deduped: dict[tuple[float, float], tuple[float, float, float]] = {}
    for (x, y), clearance in zip(points, clearances, strict=True):
        key = (round(x, _DEDUP_DIGITS), round(y, _DEDUP_DIGITS))
        if key not in deduped:
            order.append(key)
            deduped[key] = (x, y, clearance)
            continue
        if clearance > deduped[key][2]:
            deduped[key] = (x, y, clearance)

    unique_points = [(deduped[key][0], deduped[key][1]) for key in order]
    unique_clearances = [deduped[key][2] for key in order]
    return (
        np.array(unique_points, dtype=np.float64),
        np.array(unique_clearances, dtype=np.float64),
    )


def _coverage_fps_indices(
    candidates: npt.NDArray[np.float64],
    clearance: npt.NDArray[np.float64],
    clearance_weight: npt.NDArray[np.float64],
    *,
    target_count: int,
    min_radius: float,
    outline: Polygon | None,
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


def _outline_anchor_points(outline: Polygon) -> npt.NDArray[np.float64]:
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
