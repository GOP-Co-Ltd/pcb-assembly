"""銅箔島からのprobe点サンプリング."""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import numpy.typing as npt
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
    2*min_radius ステップのグリッド候補を生成。基板全体に分散させるため、
    島レベル優先のFPSを用いる。

    - 島数が target 以上の場合: 島の代表点(centroid)でFPSしtarget個の島を選び、
      各島から代表点に最も近い1点を採用。
    - 島数が target 未満の場合: 各島へ均等に枠を配分し、各島内でFPS。

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
    island_candidates = _collect_candidates(coppers, min_radius)

    total = sum(len(c) for c in island_candidates)
    if total < min_samples:
        raise ValueError(
            "probe点の候補数が min_samples に満たない: "
            f"候補数={total}, min_samples={min_samples}, "
            f"min_radius={min_radius}"
        )

    n_islands = len(island_candidates)
    target = min(max_samples, total)
    selected: list[npt.NDArray[np.float64]] = []

    if n_islands >= target:
        # 各島の代表点(centroid)でFPSしtarget島を選び、各島から代表点に最も近い候補を1点採用
        reps = np.array([cands.mean(axis=0) for cands in island_candidates])
        for idx in _fps_indices(reps, target_count=target):
            cands = island_candidates[idx]
            rep_idx = int(np.linalg.norm(cands - reps[idx], axis=1).argmin())
            selected.append(cands[rep_idx])
    else:
        # 各島へ容量上限付きで均等配分し、島内でFPS
        capacities = [len(c) for c in island_candidates]
        for cands, count in zip(
            island_candidates, _allocate_per_island(capacities, target)
        ):
            if count > 0:
                selected.extend(cands[_fps_indices(cands, target_count=count)])

    return [Point2d(x=float(p[0]), y=float(p[1])) for p in selected]


def _collect_candidates(
    coppers: Iterable[Copper], min_radius: float
) -> list[npt.NDArray[np.float64]]:
    """各島ごとの候補配列を返す（空島・候補0の島はスキップ）."""
    step = 2.0 * min_radius
    result: list[npt.NDArray[np.float64]] = []

    for copper in coppers:
        inner = copper.polygon.buffer(-min_radius)
        if inner.is_empty:
            continue

        minx, miny, maxx, maxy = inner.bounds
        xs = np.arange(minx, maxx + _BOUNDS_EPS, step)
        ys = np.arange(miny, maxy + _BOUNDS_EPS, step)
        points: list[tuple[float, float]] = []
        for x in xs:
            for y in ys:
                fx, fy = float(x), float(y)
                if inner.contains(ShapelyPoint(fx, fy)):
                    points.append((fx, fy))

        if points:
            result.append(np.array(points, dtype=np.float64))

    return result


def _allocate_per_island(capacities: list[int], target: int) -> list[int]:
    """target点をN個の島に均等配分する.

    各島の容量(capacity)を上限とし、超過分は余裕のある島に再分配する。 戻り値の合計は min(target,
    sum(capacities)) 以下。
    """
    n = len(capacities)
    if n == 0 or target <= 0:
        return [0] * n

    base, rem = divmod(target, n)
    alloc = [base + (1 if i < rem else 0) for i in range(n)]

    excess = 0
    for i in range(n):
        if alloc[i] > capacities[i]:
            excess += alloc[i] - capacities[i]
            alloc[i] = capacities[i]

    while excess > 0:
        progressed = False
        for i in range(n):
            if excess == 0:
                break
            if alloc[i] < capacities[i]:
                alloc[i] += 1
                excess -= 1
                progressed = True
        if not progressed:
            break

    return alloc


def _fps_indices(
    candidates: npt.NDArray[np.float64], *, target_count: int
) -> list[int]:
    """Farthest Point Samplingで候補のインデックスを選ぶ.

    初期点は候補群の重心から最も遠い候補（決定論的）。 以降は既選択点集合との最小距離が最大になる候補を反復選択する。
    """
    centroid = candidates.mean(axis=0)
    first_idx = int(np.linalg.norm(candidates - centroid, axis=1).argmax())

    selected = [first_idx]
    min_dist = np.linalg.norm(candidates - candidates[first_idx], axis=1)
    while len(selected) < target_count:
        next_idx = int(min_dist.argmax())
        if min_dist[next_idx] == 0.0:
            break
        selected.append(next_idx)
        new_dist = np.linalg.norm(candidates - candidates[next_idx], axis=1)
        min_dist = np.minimum(min_dist, new_dist)

    return selected
