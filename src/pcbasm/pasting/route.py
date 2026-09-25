"""Paste pad の塗布順路計算."""

from collections.abc import Iterable

import attrs

from pcbasm.geometry import Point2d, sort_by_nearest
from pcbasm.pasting.settings import PasteSettingsModel, select_enabled_pads
from pcbasm.pcb import Pad, PadHierarchy, PadShapeKey


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
) -> list[PasteRouteStop]:
    """Pad を同種類ごとにまとめ、面積の大きい順に塗布順へ並べる.

    同種類の判定は :class:`pcbasm.pcb.PadShapeKey` に従う。グループ間は
    面積降順、グループ内は直前位置からの nearest route で並べる。
    面積が同じグループどうしは長辺・短辺の降順、次に通常形状を先、最後に入力順で決める。

    Args:
        pads: 並べる pad（有効/無効の絞り込みは呼び出し側で済ませる）
        start: 最初のグループの nearest route の起点（board 座標 [mm]）
        shape_quantum: 同種類判定で面積・辺長を丸める単位 [mm / mm²]
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


def routed_enabled_pads(
    pads: Iterable[Pad], hierarchy: PadHierarchy, model: PasteSettingsModel
) -> list[Pad]:
    """有効 pad だけを通常塗布順（同種類連続・大面積優先）に並べて返す.

    :func:`pcbasm.pasting.settings.select_enabled_pads` の絞り込みと
    :func:`plan_paste_route` の順路を 1 手で適用する。順路の stop 情報
    （order / group_label / area）が要る場合は ``plan_paste_route`` を使う。
    """
    return [
        stop.pad
        for stop in plan_paste_route(select_enabled_pads(pads, hierarchy, model))
    ]
