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
    2*min_radius ステップのグリッド候補を生成。全候補を Farthest Point
    Sampling で最大 max_samples 点まで疎に選ぶ。

    Args:
        coppers: 入力の銅箔島（呼び出し側がLayerフィルタ済みを想定）
        min_radius: probe ground対応半径 [mm]
        min_samples: 最小サンプル数 (3以上を想定)
        max_samples: 最大サンプル数

    Returns:
        選ばれたprobe点 (Point2d) のリスト。FPSの選択順で返す。

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

    selected = _farthest_point_sampling(
        candidates, target_count=min(max_samples, len(candidates))
    )
    return [Point2d(x=float(x), y=float(y)) for x, y in selected]


def _collect_candidates(
    coppers: Iterable[Copper], min_radius: float
) -> npt.NDArray[np.float64]:
    """各copperをmin_radius内側にオフセットしたグリッド候補を収集する."""
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


def _farthest_point_sampling(
    candidates: npt.NDArray[np.float64], *, target_count: int
) -> npt.NDArray[np.float64]:
    """Farthest Point Samplingで候補から点を選ぶ.

    初期点は候補群の重心から最も遠い候補（決定論的）。 以降は既選択点集合との最小距離が最大になる候補を反復選択する。
    既選択点との最小距離を逐次更新することでO(N*K)の素朴計算を避ける。
    """
    centroid = candidates.mean(axis=0)
    first_idx = int(np.linalg.norm(candidates - centroid, axis=1).argmax())

    selected = [first_idx]
    min_dist = np.linalg.norm(candidates - candidates[first_idx], axis=1)
    while len(selected) < target_count:
        next_idx = int(min_dist.argmax())
        if min_dist[next_idx] == 0.0:
            break  # 残候補がすべて既選択点と一致
        selected.append(next_idx)
        new_dist = np.linalg.norm(candidates - candidates[next_idx], axis=1)
        min_dist = np.minimum(min_dist, new_dist)

    return candidates[selected]
