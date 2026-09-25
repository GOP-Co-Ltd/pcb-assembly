"""軸平行矩形のビンパッキング（MaxRects）.

複数の並び順 × 複数ヒューリスティクスを試し、使用面積が最小の配置を返す。

座標系は左上原点・右下向き正（KiCad / SVG と同じ）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

import attrs

_EPS = 1e-9

_Heuristic = Literal["short_side", "area", "bottom_left"]
_HEURISTICS: tuple[_Heuristic, ...] = ("short_side", "area", "bottom_left")


@attrs.frozen
class Rect:
    """軸平行矩形。``(x, y)`` は左上の角（Y 下向き）."""

    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    def contains(self, inner: Rect) -> bool:
        return (
            inner.x >= self.x - _EPS
            and inner.y >= self.y - _EPS
            and inner.right <= self.right + _EPS
            and inner.bottom <= self.bottom + _EPS
        )

    def intersects(self, other: Rect) -> bool:
        return not (
            self.right <= other.x + _EPS
            or other.right <= self.x + _EPS
            or self.bottom <= other.y + _EPS
            or other.bottom <= self.y + _EPS
        )


def pack_rects(
    sizes: Sequence[tuple[float, float]],
    area: Rect,
    *,
    keepouts: Sequence[Rect] = (),
    gap: float = 0.0,
) -> tuple[Rect, ...] | None:
    """``sizes`` の各 ``(width, height)`` を ``area`` 内に重ならず配置する.

    矩形は回転しない。``gap`` は配置した矩形同士の最小間隔。

    ``keepouts`` は配置禁止領域で、keepout との間隔 ``gap`` は保証しない。

    keepout との間隔が要るなら、呼び出し側で keepout を ``gap`` だけ広げて渡す。

    入力順と複数のサイズ順ソートに 3 種のヒューリスティクスを掛け合わせて試す。

    その中で使用外接面積が最小の配置を選ぶ。

    Returns:
        ``sizes`` と同じ順の配置矩形。空入力なら空タプル、収まらなければ ``None``
    """
    if not sizes:
        return ()
    attempts: list[dict[int, Rect]] = []
    for order in _orders(sizes):
        for heuristic in _HEURISTICS:
            packed = _pack_max_rects(sizes, order, area, keepouts, gap, heuristic)
            if packed is not None:
                attempts.append(packed)
    if not attempts:
        return None
    best = min(attempts, key=lambda packed: _score(packed, area))
    return tuple(best[index] for index in range(len(sizes)))


def _orders(sizes: Sequence[tuple[float, float]]) -> tuple[tuple[int, ...], ...]:
    indices = tuple(range(len(sizes)))
    sort_keys = (
        lambda i: -sizes[i][0] * sizes[i][1],
        lambda i: -max(sizes[i]),
        lambda i: -sizes[i][0],
        lambda i: -sizes[i][1],
        lambda i: -(sizes[i][0] + sizes[i][1]),
    )
    orders = [indices]
    seen = {indices}
    for key in sort_keys:
        order = tuple(sorted(indices, key=key))
        if order not in seen:
            orders.append(order)
            seen.add(order)
    return tuple(orders)


def _pack_max_rects(
    sizes: Sequence[tuple[float, float]],
    order: Sequence[int],
    area: Rect,
    keepouts: Sequence[Rect],
    gap: float,
    heuristic: _Heuristic,
) -> dict[int, Rect] | None:
    free_rectangles: tuple[Rect, ...] = (
        Rect(area.x, area.y, area.width + gap, area.height + gap),
    )
    for keepout in keepouts:
        free_rectangles = _split_free_rectangles(free_rectangles, keepout)
    placements: dict[int, Rect] = {}
    for index in order:
        width, height = sizes[index]
        packed_width = width + gap
        packed_height = height + gap
        choices: list[tuple[tuple[float, ...], Rect]] = []
        for free in free_rectangles:
            if packed_width > free.width + _EPS or packed_height > free.height + _EPS:
                continue
            score = _choice_score(
                heuristic,
                free,
                packed_width,
                packed_height,
                free.width - packed_width,
                free.height - packed_height,
            )
            choices.append((score, free))
        if not choices:
            return None
        _score_value, free = min(choices, key=lambda item: item[0])
        used = Rect(free.x, free.y, packed_width, packed_height)
        placements[index] = Rect(free.x, free.y, width, height)
        free_rectangles = _split_free_rectangles(free_rectangles, used)
    return placements


def _choice_score(
    heuristic: _Heuristic,
    free: Rect,
    width: float,
    height: float,
    remaining_width: float,
    remaining_height: float,
) -> tuple[float, ...]:
    short_side = min(remaining_width, remaining_height)
    long_side = max(remaining_width, remaining_height)
    area_waste = free.width * free.height - width * height
    suffix = (free.y, free.x)
    if heuristic == "area":
        return (area_waste, short_side, long_side, *suffix)
    if heuristic == "bottom_left":
        return (free.y + height, free.x, short_side, long_side)
    return (short_side, long_side, area_waste, *suffix)


def _split_free_rectangles(
    free_rectangles: tuple[Rect, ...], used: Rect
) -> tuple[Rect, ...]:
    split: list[Rect] = []
    for free in free_rectangles:
        if not free.intersects(used):
            split.append(free)
            continue
        if used.x > free.x + _EPS:
            split.append(Rect(free.x, free.y, used.x - free.x, free.height))
        if used.right < free.right - _EPS:
            split.append(Rect(used.right, free.y, free.right - used.right, free.height))
        if used.y > free.y + _EPS:
            split.append(Rect(free.x, free.y, free.width, used.y - free.y))
        if used.bottom < free.bottom - _EPS:
            split.append(
                Rect(free.x, used.bottom, free.width, free.bottom - used.bottom)
            )
    return _prune_free_rectangles(split)


def _prune_free_rectangles(rectangles: list[Rect]) -> tuple[Rect, ...]:
    # 同一または許容誤差内で相互包含する矩形は、先に出た一方だけを残す。
    useful = tuple(
        dict.fromkeys(
            rectangle
            for rectangle in rectangles
            if rectangle.width > _EPS and rectangle.height > _EPS
        )
    )
    return tuple(
        rectangle
        for index, rectangle in enumerate(useful)
        if not any(
            index != other_index
            and other.contains(rectangle)
            and (not rectangle.contains(other) or other_index < index)
            for other_index, other in enumerate(useful)
        )
    )


def _score(placements: Mapping[int, Rect], area: Rect) -> tuple[object, ...]:
    used_width = max(item.right for item in placements.values()) - area.x
    used_height = max(item.bottom for item in placements.values()) - area.y
    positions = tuple(
        (index, round(item.y, 9), round(item.x, 9))
        for index, item in sorted(placements.items())
    )
    return used_width * used_height, used_height, used_width, positions
