"""銅箔島からのprobe点サンプリング."""

from __future__ import annotations

from collections.abc import Iterable
from itertools import combinations

import numpy as np
import numpy.typing as npt
from scipy.spatial import ConvexHull, QhullError
from shapely.geometry import Point as ShapelyPoint

from pcb_assembly.geometry.transform import Point2d
from pcb_assembly.pcb.board import Copper

_BOUNDS_EPS = 1e-9


def sample_points_in_coppers(
    coppers: Iterable[Copper],
    *,
    min_radius: float,
    min_samples: int,
    max_samples: int,
) -> list[Point2d]:
    """銅箔島の内部からprobe用の点をできる限りまばらにサンプルする.

    各島の polygon を min_radius だけ内側にオフセットし、その領域内に
    2*min_radius ステップのグリッド候補を生成。基板全体に3点を広く張るため、
    まず候補から辺の和(周長)が最大の三角形を成す3点をseedとして選び、その後
    Farthest Point Samplingで残り (max_samples - 3) 点を密度均等になるよう追加する。

    Args:
        coppers: 入力の銅箔島（呼び出し側がLayerフィルタ済みを想定）
        min_radius: probe ground対応半径 [mm]
        min_samples: 最小サンプル数 (3以上を想定)
        max_samples: 最大サンプル数

    Returns:
        選ばれたprobe点 (Point2d) のリスト。

    Raises:
        ValueError: 候補点が min_samples に満たない場合
    """
    candidates = _collect_candidates(coppers, min_radius)

    if len(candidates) < min_samples:
        raise ValueError(
            "probe点の候補数が min_samples に満たない: "
            f"候補数={len(candidates)}, min_samples={min_samples}, "
            f"min_radius={min_radius}"
        )

    target = min(max_samples, len(candidates))
    seed = _max_perimeter_triangle_indices(candidates)[:target]
    selected = _fps_indices(candidates, target_count=target, seed_indices=seed)
    return [Point2d(x=float(p[0]), y=float(p[1])) for p in candidates[selected]]


def _collect_candidates(
    coppers: Iterable[Copper], min_radius: float
) -> npt.NDArray[np.float64]:
    """各copperをmin_radius内側にオフセットしたグリッド候補を全島まとめて収集する."""
    step = 2.0 * min_radius
    points: list[tuple[float, float]] = []

    for copper in coppers:
        inner = copper.polygon.buffer(-min_radius)
        if inner.is_empty:
            continue

        minx, miny, maxx, maxy = inner.bounds
        xs = np.arange(minx, maxx + _BOUNDS_EPS, step)
        ys = np.arange(miny, maxy + _BOUNDS_EPS, step)
        for x in xs:
            for y in ys:
                fx, fy = float(x), float(y)
                if inner.contains(ShapelyPoint(fx, fy)):
                    points.append((fx, fy))

    if not points:
        return np.empty((0, 2), dtype=np.float64)
    return np.array(points, dtype=np.float64)


def _max_perimeter_triangle_indices(candidates: npt.NDArray[np.float64]) -> list[int]:
    """候補(3点以上)から周長最大の三角形を成す3点のインデックスを返す.

    周長最大三角形は必ず凸包頂点上にあるため、ConvexHullを取り頂点間の三重ループ O(H^3)
    で探索する。全候補が共線等で凸包が2D化できない場合は先頭3点を返す。
    """
    try:
        hull_idxs = [int(i) for i in ConvexHull(candidates).vertices]
    except QhullError:
        return [0, 1, 2]

    if len(hull_idxs) < 3:
        return [0, 1, 2]

    pts = candidates[hull_idxs]
    best_perimeter = -1.0
    best = (hull_idxs[0], hull_idxs[1], hull_idxs[2])
    for i, j, k in combinations(range(len(hull_idxs)), 3):
        a, b, c = pts[i], pts[j], pts[k]
        perimeter = (
            float(np.linalg.norm(b - a))
            + float(np.linalg.norm(c - b))
            + float(np.linalg.norm(c - a))
        )
        if perimeter > best_perimeter:
            best_perimeter = perimeter
            best = (hull_idxs[i], hull_idxs[j], hull_idxs[k])
    return list(best)


def _fps_indices(
    candidates: npt.NDArray[np.float64],
    *,
    target_count: int,
    seed_indices: Iterable[int],
) -> list[int]:
    """Seed集合を初期選択としてFarthest Point Samplingでインデックスを選ぶ.

    既選択点集合との最小距離が最大になる候補を反復選択し、target_count に達するまで追加する。
    """
    selected = list(seed_indices)
    min_dist = np.full(len(candidates), np.inf)
    for idx in selected:
        d = np.linalg.norm(candidates - candidates[idx], axis=1)
        min_dist = np.minimum(min_dist, d)

    while len(selected) < target_count:
        next_idx = int(min_dist.argmax())
        if min_dist[next_idx] == 0.0:
            break
        selected.append(next_idx)
        new_dist = np.linalg.norm(candidates - candidates[next_idx], axis=1)
        min_dist = np.minimum(min_dist, new_dist)

    return selected
