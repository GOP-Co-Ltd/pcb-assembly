from collections.abc import Iterable

from .transform import Point3d


def sort_by_nearest(positions: Iterable[Point3d], start: Point3d) -> list[Point3d]:
    """開始点からの巡回経路を最適化する（nearest neighbor + 2-opt）.

    Args:
        positions: 並べ替える位置のシーケンス
        start: 開始点

    Returns:
        開始点からの巡回経路として最適化された位置のリスト
    """
    remaining = list(positions)
    if len(remaining) <= 1:
        return remaining

    # Step 1: Nearest Neighborで初期ツアーを生成
    route: list[Point3d] = []
    current = start
    while remaining:
        nearest_idx = min(
            range(len(remaining)), key=lambda i: (remaining[i] - current).norm()
        )
        nearest = remaining.pop(nearest_idx)
        route.append(nearest)
        current = nearest

    # Step 2: 2-optで改善
    return _apply_2opt(route, start)


def _apply_2opt(route: list[Point3d], start: Point3d) -> list[Point3d]:
    """2-opt局所探索でopen-path経路を改善する."""
    n = len(route)
    improved = True
    while improved:
        improved = False
        for i in range(n - 1):
            for j in range(i + 1, n):
                # 現在の辺のコスト
                prev_i = start if i == 0 else route[i - 1]
                old_cost = (route[i] - prev_i).norm()
                if j + 1 < n:
                    old_cost += (route[j + 1] - route[j]).norm()

                # 反転後の辺のコスト
                new_cost = (route[j] - prev_i).norm()
                if j + 1 < n:
                    new_cost += (route[j + 1] - route[i]).norm()

                if new_cost < old_cost:
                    route[i : j + 1] = route[i : j + 1][::-1]
                    improved = True
    return route
