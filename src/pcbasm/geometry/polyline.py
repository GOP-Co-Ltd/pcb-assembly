"""点列（ポリライン）ユーティリティ."""

from __future__ import annotations

from collections.abc import Sequence

from .transform import Point2d


def polyline_length(points: Sequence[Point2d]) -> float:
    """ポリラインの総延長を返す（点が 2 未満なら 0）."""
    return sum((points[i + 1] - points[i]).norm for i in range(len(points) - 1))


def ring_segment(
    vertices: Sequence[Point2d], i_start: int, i_end: int, *, step: int
) -> list[Point2d]:
    """巡回頂点列 ``vertices`` を ``i_start`` から ``i_end`` まで ``step`` 方向に辿った点列.

    ``step`` は ``+1``（順方向）または ``-1``（逆方向）。

    末尾から先頭へ折り返して辿る。戻り値は両端を含む。
    """
    n = len(vertices)
    path: list[Point2d] = []
    i = i_start
    while i != i_end:
        path.append(vertices[i])
        i = (i + step) % n
    path.append(vertices[i_end])
    return path
