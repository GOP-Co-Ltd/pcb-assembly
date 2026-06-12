from collections.abc import Callable, Iterable
from typing import cast, overload

from .transform import Point3d


@overload
def sort_by_nearest(positions: Iterable[Point3d], start: Point3d) -> list[Point3d]: ...


@overload
def sort_by_nearest[T](
    positions: Iterable[T], start: Point3d, *, key: Callable[[T], Point3d]
) -> list[T]: ...


def sort_by_nearest[T](
    positions: Iterable[T],
    start: Point3d,
    *,
    key: Callable[[T], Point3d] | None = None,
) -> list[T]:
    """開始点からの巡回経路を最適化する（nearest neighbor + 2-opt）.

    Args:
        positions: 並べ替えるアイテムのシーケンス
        start: 開始点
        key: アイテムから位置を取り出す関数。省略時はアイテム自身を
            Point3dとして扱う

    Returns:
        開始点からの巡回経路として最適化されたアイテムのリスト
    """
    point_of = key if key is not None else cast(Callable[[T], Point3d], lambda x: x)
    remaining = list(positions)
    if len(remaining) <= 1:
        return remaining

    # Step 1: Nearest Neighborで初期ツアーを生成
    route: list[T] = []
    current = start
    while remaining:
        nearest_idx = min(
            range(len(remaining)),
            key=lambda i: (point_of(remaining[i]) - current).norm(),
        )
        nearest = remaining.pop(nearest_idx)
        route.append(nearest)
        current = point_of(nearest)

    # Step 2: 2-optで改善
    return _apply_2opt(route, start, point_of)


def _apply_2opt[T](
    route: list[T], start: Point3d, point_of: Callable[[T], Point3d]
) -> list[T]:
    """2-opt局所探索でopen-path経路を改善する."""
    n = len(route)
    improved = True
    while improved:
        improved = False
        for i in range(n - 1):
            for j in range(i + 1, n):
                # 現在の辺のコスト
                prev_i = start if i == 0 else point_of(route[i - 1])
                old_cost = (point_of(route[i]) - prev_i).norm()
                if j + 1 < n:
                    old_cost += (point_of(route[j + 1]) - point_of(route[j])).norm()

                # 反転後の辺のコスト
                new_cost = (point_of(route[j]) - prev_i).norm()
                if j + 1 < n:
                    new_cost += (point_of(route[j + 1]) - point_of(route[i])).norm()

                if new_cost < old_cost:
                    route[i : j + 1] = route[i : j + 1][::-1]
                    improved = True
    return route
