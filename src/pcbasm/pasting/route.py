"""Paste pad の塗布順路計算."""

from collections.abc import Callable, Iterable

import attrs

from pcbasm.board import Pad
from pcbasm.geometry import Point2d, sort_by_nearest
from pcbasm.pasting.hierarchy import PadShapeKey


@attrs.frozen
class PasteRouteStop:
    """塗布順路上の 1 pad.

    Attributes:
        pad: 塗る pad
        order: 塗布順（1 始まり）
        group_label: 同種類グループの表示名（例 ``"0.50x0.90mm"``）
        area: pad 面積 [mm²]
    """

    pad: Pad
    order: int
    group_label: str
    area: float


def plan_paste_route(
    pads: Iterable[Pad],
    *,
    start: Point2d = Point2d(0.0, 0.0),
    shape_quantum: float = 0.01,
    paste_order: Callable[[Pad], int] = lambda _: 0,
) -> list[PasteRouteStop]:
    """Pad を塗布順序と同種類ごとにまとめ、順序・面積の小さい順に塗布順へ並べる.

    同種類の判定は :class:`pcbasm.pasting.hierarchy.PadShapeKey` に従う。グループ間は
    塗布順序の昇順、同じ順序の中は面積昇順、グループ内は直前位置からの nearest route で並べる。
    面積が同じグループどうしは長辺・短辺の昇順、次に通常形状を先、最後に入力順で決める。

    Args:
        pads: 並べる pad（有効/無効の絞り込みは呼び出し側で済ませる）
        start: 最初のグループの nearest route の起点（board 座標 [mm]）
        shape_quantum: 同種類判定で面積・辺長を丸める単位 [mm / mm²]
        paste_order: pad の塗布順序（小さいほど先。既定は全 pad 同じ）
    """
    groups: dict[tuple[int, PadShapeKey], list[Pad]] = {}
    first_index: dict[tuple[int, PadShapeKey], int] = {}
    for index, pad in enumerate(pads):
        key = (paste_order(pad), PadShapeKey.of(pad, quantum=shape_quantum))
        groups.setdefault(key, []).append(pad)
        first_index.setdefault(key, index)

    def sort_key(key: tuple[int, PadShapeKey]) -> tuple[int, int, int, int, bool, int]:
        paste_order, shape = key
        return (
            paste_order,
            shape.area_q,
            shape.long_q,
            shape.short_q,
            shape.is_custom_shape,
            first_index[key],
        )

    route: list[PasteRouteStop] = []
    current = start.to3d()
    order = 1
    for key in sorted(groups, key=sort_key):
        _, shape = key
        group = sort_by_nearest(
            groups[key],
            current,
            key=lambda pad: pad.center.to3d(),
        )
        for pad in group:
            route.append(
                PasteRouteStop(
                    pad=pad,
                    order=order,
                    group_label=shape.label,
                    area=pad.area,
                )
            )
            order += 1
        if group:
            current = group[-1].center.to3d()
    return route
