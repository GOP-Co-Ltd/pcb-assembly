"""Paste pad の塗布順路計算."""

from collections.abc import Iterable

import attrs

from pcbasm.geometry import Point2d, sort_by_nearest
from pcbasm.pcb import Pad, PadShapeKey


@attrs.frozen
class PasteRouteStop:
    """塗布順路上の 1 pad."""

    pad: Pad
    order: int
    group_label: str
    area: float


def plan_paste_route(
    pads: Iterable[Pad],
    *,
    start: Point2d = Point2d(0.0, 0.0),
    shape_quantum: float = 0.01,
) -> list[PasteRouteStop]:
    """Pad を同種類ごとにまとめ、面積の大きい順に塗布順へ並べる.

    同種類の判定は :class:`pcbasm.pcb.PadShapeKey` に従う。グループ間は
    面積降順、グループ内は直前位置からの nearest route で並べる。
    """
    groups: dict[PadShapeKey, list[Pad]] = {}
    first_index: dict[PadShapeKey, int] = {}
    for index, pad in enumerate(pads):
        key = PadShapeKey.of(pad, quantum=shape_quantum)
        groups.setdefault(key, []).append(pad)
        first_index.setdefault(key, index)

    ordered_keys = sorted(
        groups,
        key=lambda key: (
            -key.area_q,
            -key.long_q,
            -key.short_q,
            key.is_custom_shape,
            first_index[key],
        ),
    )

    route: list[PasteRouteStop] = []
    current = start.to3d()
    order = 1
    for key in ordered_keys:
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
                    group_label=key.label,
                    area=pad.area,
                )
            )
            order += 1
        if group:
            current = group[-1].center.to3d()
    return route
