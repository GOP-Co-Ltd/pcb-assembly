"""ポリゴン領域からのprobe点サンプリング."""

from __future__ import annotations

from collections.abc import Iterable
from itertools import product

import numpy as np
import numpy.typing as npt
from shapely import get_parts
from shapely.geometry import Point as ShapelyPoint, Polygon
from shapely.ops import polylabel

from pcbasm.geometry.transform import Point2d

_BOUNDS_EPS = 1e-9


def sample_points_in_polygons(
    polygons: Iterable[Polygon],
    *,
    min_radius: float,
    min_samples: int,
    max_samples: int,
) -> list[Point2d]:
    """ポリゴン領域の内部からprobe用の点を安全かつ広く分散するようサンプルする.

    各ポリゴンを min_radius だけ内側にオフセットした領域内に、min_radius ステップの
    グリッド候補と各連結部分の中心(pole of inaccessibility)を生成する。各候補には
    銅箔境界からのクリアランス(距離)を付与し、クリアランス重み付きの反復貪欲法
    (weighted Farthest Point Sampling)で選ぶ。これにより「銅箔境界から十分内側
    (安全)」かつ「互いに広く離れる(被覆面積が大きい)」点が得られる。

    Args:
        polygons: 入力ポリゴン群（例: 銅箔島のpolygon。呼び出し側でフィルタ済みを想定）
        min_radius: probe ground対応半径 [mm]
        min_samples: 最小サンプル数 (3以上を想定)
        max_samples: 最大サンプル数

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
    selected = _weighted_fps_indices(candidates, clearance, target_count=target)
    return [Point2d(x=float(x), y=float(y)) for x, y in candidates[selected]]


def _collect_candidates(
    polygons: Iterable[Polygon], min_radius: float
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """min_radius内側にオフセットした領域の候補点と、各点のクリアランスを収集する.

    各polygonを min_radius 内側にオフセットし、min_radius ステップのグリッド点に加えて
    各連結部分の中心 (pole of inaccessibility) を候補に含める。クリアランスは元の
    ``polygon.boundary`` (穴を含む) からの距離で、銅箔境界からの安全余裕を表す。
    全候補は ``buffer(-min_radius)`` の内部にあるため最小クリアランス min_radius を満たす。
    """
    step = min_radius
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
            center = polylabel(part, tolerance=min_radius / 10.0)
            points.append((center.x, center.y))
            clearances.append(boundary.distance(center))

    if not points:
        return (
            np.empty((0, 2), dtype=np.float64),
            np.empty((0,), dtype=np.float64),
        )
    return (
        np.array(points, dtype=np.float64),
        np.array(clearances, dtype=np.float64),
    )


def _weighted_fps_indices(
    candidates: npt.NDArray[np.float64],
    clearance: npt.NDArray[np.float64],
    *,
    target_count: int,
) -> list[int]:
    """クリアランス重み付き反復貪欲法でインデックスを選ぶ.

    初手は最もクリアランスの高い(最も安全な)点を選ぶ。以降は既選択集合への最小距離
    ``d`` (被覆面積の伸びの代理量) と ``clearance`` (安全性) の積を最大化する候補を
    反復選択する。両者とも大きいほど良い量の積なので、重みパラメータなしで安全性と
    被覆を同時に最大化する。
    """
    first = int(clearance.argmax())
    selected = [first]
    min_dist = np.linalg.norm(candidates - candidates[first], axis=1)

    while len(selected) < target_count:
        next_idx = int((min_dist * clearance).argmax())
        if min_dist[next_idx] == 0.0:
            break
        selected.append(next_idx)
        new_dist = np.linalg.norm(candidates - candidates[next_idx], axis=1)
        min_dist = np.minimum(min_dist, new_dist)

    return selected
