"""解決済みpreview DTOとパッド単位の自動最適配置."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, TypeAlias

import attrs
import pcbnew

from .catalog import (
    PasteFlowCalibrationFootprintEnvelope,
    PasteFlowCalibrationResolvedPadPattern,
    duplicate_footprint,
    effective_pad_polygon,
    footprint_envelope,
    to_mm,
    vector,
)
from .config import (
    PasteFlowCalibrationBoardConfig,
    PasteFlowCalibrationBoardOverflowError,
    PasteFlowCalibrationBoardSpec,
    PasteFlowCalibrationPreviewLayer,
)


@attrs.frozen
class PasteFlowCalibrationPoint:
    """基板左上原点の2D座標 [mm]."""

    x: float
    y: float


@attrs.frozen
class PasteFlowCalibrationPolygon:
    """preview用の解決済みパッドポリゴン."""

    layer: PasteFlowCalibrationPreviewLayer
    points: tuple[PasteFlowCalibrationPoint, ...]


@attrs.frozen
class PasteFlowCalibrationBounds:
    """基板左上原点の矩形 [mm]."""

    x: float
    y: float
    width: float
    height: float


@attrs.frozen
class PasteFlowCalibrationPadLayout:
    """生成する単一パッドfootprintの解決済み配置."""

    catalog_id: str
    display_name: str
    reference: str
    bounds: PasteFlowCalibrationBounds
    x: float
    y: float
    rotation_deg: float
    polygons: tuple[PasteFlowCalibrationPolygon, ...]


@attrs.frozen
class PasteFlowCalibrationPatternLayout:
    """パッド設定ごとの解決済み回転角."""

    catalog_id: str
    angles_deg: tuple[float, ...]


@attrs.frozen
class PasteFlowCalibrationBoardLayout:
    """WebUIとKiCad生成が共有する完全に解決済みの配置."""

    board: PasteFlowCalibrationBoardSpec
    placement_area: PasteFlowCalibrationBounds
    preview_bounds: PasteFlowCalibrationBounds
    purge_pad: PasteFlowCalibrationBounds
    purge_polygons: tuple[PasteFlowCalibrationPolygon, ...]
    patterns: tuple[PasteFlowCalibrationPatternLayout, ...]
    pads: tuple[PasteFlowCalibrationPadLayout, ...]

    @property
    def pad_count(self) -> int:
        return len(self.pads)


@attrs.frozen
class _PackingRect:
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


@attrs.frozen
class _PadToPack:
    index: int
    catalog_id: str
    display_name: str
    rotation_deg: float
    envelope: PasteFlowCalibrationFootprintEnvelope

    @property
    def width(self) -> float:
        return self.envelope.width

    @property
    def height(self) -> float:
        return self.envelope.height


_PackingHeuristic: TypeAlias = Literal["short_side", "area", "bottom_left"]


def build_paste_flow_calibration_board_layout(
    config: PasteFlowCalibrationBoardConfig,
    resolved: Mapping[str, PasteFlowCalibrationResolvedPadPattern],
) -> PasteFlowCalibrationBoardLayout:
    """解決済みconfigとtemplateから配置可能なlayoutを構築する."""

    patterns, pads = _pads_to_pack(config, resolved)
    placements = _pack_pads(config, pads)
    return _build_layout(config, resolved, patterns, pads, placements)


def preview_paste_flow_calibration_board_layout(
    config: PasteFlowCalibrationBoardConfig,
    resolved: Mapping[str, PasteFlowCalibrationResolvedPadPattern],
) -> tuple[PasteFlowCalibrationBoardLayout, str | None]:
    """超過時も全パッドを含む診断用layoutと理由を返す."""

    patterns, pads = _pads_to_pack(config, resolved)
    try:
        placements = _pack_pads(config, pads)
    except PasteFlowCalibrationBoardOverflowError as exc:
        placements = _pack_pads_for_overflow_preview(config, pads)
        overflow_message: str | None = str(exc)
    else:
        overflow_message = None
    return (
        _build_layout(config, resolved, patterns, pads, placements),
        overflow_message,
    )


def _pads_to_pack(
    config: PasteFlowCalibrationBoardConfig,
    resolved: Mapping[str, PasteFlowCalibrationResolvedPadPattern],
) -> tuple[tuple[PasteFlowCalibrationPatternLayout, ...], tuple[_PadToPack, ...]]:
    layouts: list[PasteFlowCalibrationPatternLayout] = []
    pads: list[_PadToPack] = []
    for pattern in config.patterns:
        resolved_pattern = resolved[pattern.catalog_id]
        angles = tuple(
            index * pattern.rotation_span_deg / pattern.rotation_count
            for index in range(pattern.rotation_count)
        )
        envelopes = tuple(
            footprint_envelope(resolved_pattern.template, angle) for angle in angles
        )
        layouts.append(PasteFlowCalibrationPatternLayout(pattern.catalog_id, angles))
        display_name = (
            f"{resolved_pattern.item.footprint_label} / {resolved_pattern.item.label}"
        )
        for _repeat_index in range(pattern.repeat_count):
            for angle, envelope in zip(angles, envelopes, strict=True):
                pads.append(
                    _PadToPack(
                        index=len(pads),
                        catalog_id=pattern.catalog_id,
                        display_name=display_name,
                        rotation_deg=angle,
                        envelope=envelope,
                    )
                )
    return tuple(layouts), tuple(pads)


def _build_layout(
    config: PasteFlowCalibrationBoardConfig,
    resolved: Mapping[str, PasteFlowCalibrationResolvedPadPattern],
    patterns: tuple[PasteFlowCalibrationPatternLayout, ...],
    pads: tuple[_PadToPack, ...],
    placements: Mapping[int, PasteFlowCalibrationBounds],
) -> PasteFlowCalibrationBoardLayout:
    pad_layouts: list[PasteFlowCalibrationPadLayout] = []
    for pad in pads:
        bounds = placements[pad.index]
        anchor_x = bounds.x - pad.envelope.min_x
        anchor_y = bounds.y - pad.envelope.min_y
        placed = duplicate_footprint(resolved[pad.catalog_id].template)
        placed.SetPosition(vector(anchor_x, anchor_y))
        placed.SetOrientationDegrees(pad.rotation_deg)
        pad_layouts.append(
            PasteFlowCalibrationPadLayout(
                catalog_id=pad.catalog_id,
                display_name=pad.display_name,
                reference=f"PAD{pad.index + 1}",
                bounds=bounds,
                x=anchor_x,
                y=anchor_y,
                rotation_deg=pad.rotation_deg,
                polygons=_footprint_polygons(placed),
            )
        )
    purge = PasteFlowCalibrationBounds(
        x=config.board.edge_margin_mm,
        y=config.board.edge_margin_mm,
        width=config.purge_pad.width_mm,
        height=config.purge_pad.height_mm,
    )
    purge_points = _rectangle_points(purge)
    placement_area = _placement_area(config)
    preview_bounds = _preview_bounds(config.board, purge, pad_layouts)
    return PasteFlowCalibrationBoardLayout(
        board=config.board,
        placement_area=placement_area,
        preview_bounds=preview_bounds,
        purge_pad=purge,
        purge_polygons=(
            PasteFlowCalibrationPolygon("F.Cu", purge_points),
            PasteFlowCalibrationPolygon("F.Paste", purge_points),
        ),
        patterns=patterns,
        pads=tuple(pad_layouts),
    )


def _preview_bounds(
    board: PasteFlowCalibrationBoardSpec,
    purge: PasteFlowCalibrationBounds,
    pads: list[PasteFlowCalibrationPadLayout],
) -> PasteFlowCalibrationBounds:
    bounds = (
        PasteFlowCalibrationBounds(0.0, 0.0, board.width_mm, board.height_mm),
        purge,
        *(pad.bounds for pad in pads),
    )
    left = min(item.x for item in bounds)
    top = min(item.y for item in bounds)
    right = max(item.x + item.width for item in bounds)
    bottom = max(item.y + item.height for item in bounds)
    return PasteFlowCalibrationBounds(left, top, right - left, bottom - top)


def _pack_pads(
    config: PasteFlowCalibrationBoardConfig,
    pads: tuple[_PadToPack, ...],
) -> dict[int, PasteFlowCalibrationBounds]:
    _validate_purge_region(config)
    area = _packing_area(config)
    for pad in pads:
        if pad.width > area.width + 1e-9 or pad.height > area.height + 1e-9:
            raise PasteFlowCalibrationBoardOverflowError(
                f"{pad.display_name}のパッド（{pad.width:.2f} × {pad.height:.2f} mm）が"
                f"配置領域{area.width:.2f} × {area.height:.2f} mmに収まりません"
            )
    packed = _find_optimized_packing(
        pads,
        area,
        _purge_keepout(config),
        config.board.pad_gap_mm,
    )
    if packed is None:
        raise PasteFlowCalibrationBoardOverflowError(
            "自動最適配置でもすべてのパッドが基板の配置可能領域に収まりません"
        )
    return packed


def _pack_pads_for_overflow_preview(
    config: PasteFlowCalibrationBoardConfig,
    pads: tuple[_PadToPack, ...],
) -> dict[int, PasteFlowCalibrationBounds]:
    area = _overflow_preview_area(config, pads)
    packed = _find_optimized_packing(
        pads,
        area,
        _purge_keepout(config),
        config.board.pad_gap_mm,
    )
    if packed is not None:
        return packed
    return _stack_pads_for_overflow_preview(config, pads)


def _validate_purge_region(config: PasteFlowCalibrationBoardConfig) -> None:
    board = config.board
    available_width = board.width_mm - 2 * board.edge_margin_mm
    available_height = board.height_mm - 2 * board.edge_margin_mm
    if config.purge_pad.width_mm > available_width + 1e-9:
        raise PasteFlowCalibrationBoardOverflowError(
            f"purge pad幅{config.purge_pad.width_mm:.2f} mmが"
            f"配置可能幅{available_width:.2f} mmを超えます"
        )
    if config.purge_pad.height_mm > available_height + 1e-9:
        raise PasteFlowCalibrationBoardOverflowError(
            f"purge pad高さ{config.purge_pad.height_mm:.2f} mmが"
            f"配置可能高さ{available_height:.2f} mmを超えます"
        )


def _packing_area(config: PasteFlowCalibrationBoardConfig) -> _PackingRect:
    board = config.board
    return _PackingRect(
        x=board.edge_margin_mm,
        y=board.edge_margin_mm,
        width=board.width_mm - 2 * board.edge_margin_mm,
        height=board.height_mm - 2 * board.edge_margin_mm,
    )


def _placement_area(
    config: PasteFlowCalibrationBoardConfig,
) -> PasteFlowCalibrationBounds:
    area = _packing_area(config)
    return PasteFlowCalibrationBounds(area.x, area.y, area.width, area.height)


def _purge_keepout(config: PasteFlowCalibrationBoardConfig) -> _PackingRect:
    area = _packing_area(config)
    return _PackingRect(
        x=area.x,
        y=area.y,
        width=config.purge_pad.width_mm + config.board.pad_gap_mm,
        height=config.purge_pad.height_mm + config.board.pad_gap_mm,
    )


def _overflow_preview_area(
    config: PasteFlowCalibrationBoardConfig,
    pads: tuple[_PadToPack, ...],
) -> _PackingRect:
    area = _packing_area(config)
    purge_keepout = _purge_keepout(config)
    width = max(area.width, purge_keepout.width, *(pad.width for pad in pads))
    stacked_height = purge_keepout.height + sum(
        pad.height + config.board.pad_gap_mm for pad in pads
    )
    return _PackingRect(
        x=area.x,
        y=area.y,
        width=width,
        height=max(area.height, stacked_height),
    )


def _stack_pads_for_overflow_preview(
    config: PasteFlowCalibrationBoardConfig,
    pads: tuple[_PadToPack, ...],
) -> dict[int, PasteFlowCalibrationBounds]:
    area = _packing_area(config)
    y = max(area.y, _purge_keepout(config).bottom)
    placements: dict[int, PasteFlowCalibrationBounds] = {}
    for pad in pads:
        placements[pad.index] = PasteFlowCalibrationBounds(
            x=area.x,
            y=y,
            width=pad.width,
            height=pad.height,
        )
        y += pad.height + config.board.pad_gap_mm
    return placements


def _find_optimized_packing(
    pads: tuple[_PadToPack, ...],
    area: _PackingRect,
    purge_keepout: _PackingRect,
    gap: float,
) -> dict[int, PasteFlowCalibrationBounds] | None:
    attempts: list[dict[int, PasteFlowCalibrationBounds]] = []
    heuristics: tuple[_PackingHeuristic, ...] = (
        "short_side",
        "area",
        "bottom_left",
    )
    for order in _packing_orders(pads):
        for heuristic in heuristics:
            packed = _pack_max_rects(
                order,
                area,
                purge_keepout,
                gap,
                heuristic,
            )
            if packed is not None:
                attempts.append(packed)
    if not attempts:
        return None
    return min(attempts, key=lambda packed: _packing_score(packed, area))


def _packing_orders(
    pads: tuple[_PadToPack, ...],
) -> tuple[tuple[_PadToPack, ...], ...]:
    sort_keys = (
        lambda pad: (-pad.width * pad.height,),
        lambda pad: (-max(pad.width, pad.height),),
        lambda pad: (-pad.width,),
        lambda pad: (-pad.height,),
        lambda pad: (-(pad.width + pad.height),),
    )
    orders = [pads]
    seen = {tuple(pad.index for pad in pads)}
    for key in sort_keys:
        order = tuple(sorted(pads, key=key))
        identity = tuple(pad.index for pad in order)
        if identity not in seen:
            orders.append(order)
            seen.add(identity)
    return tuple(orders)


def _pack_max_rects(
    pads: tuple[_PadToPack, ...],
    area: _PackingRect,
    purge_keepout: _PackingRect,
    gap: float,
    heuristic: _PackingHeuristic,
) -> dict[int, PasteFlowCalibrationBounds] | None:
    free_rectangles = _split_free_rectangles(
        (_PackingRect(area.x, area.y, area.width + gap, area.height + gap),),
        purge_keepout,
    )
    placements: dict[int, PasteFlowCalibrationBounds] = {}
    for pad in pads:
        packed_width = pad.width + gap
        packed_height = pad.height + gap
        choices: list[tuple[tuple[float, ...], _PackingRect]] = []
        for free in free_rectangles:
            if packed_width > free.width + 1e-9 or packed_height > free.height + 1e-9:
                continue
            remaining_width = free.width - packed_width
            remaining_height = free.height - packed_height
            choices.append(
                (
                    _max_rects_choice_score(
                        heuristic,
                        free,
                        packed_width,
                        packed_height,
                        remaining_width,
                        remaining_height,
                    ),
                    free,
                )
            )
        if not choices:
            return None
        _score, free = min(choices, key=lambda item: item[0])
        used = _PackingRect(free.x, free.y, packed_width, packed_height)
        placements[pad.index] = PasteFlowCalibrationBounds(
            free.x,
            free.y,
            pad.width,
            pad.height,
        )
        free_rectangles = _split_free_rectangles(free_rectangles, used)
    return placements


def _max_rects_choice_score(
    heuristic: _PackingHeuristic,
    free: _PackingRect,
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
    free_rectangles: tuple[_PackingRect, ...], used: _PackingRect
) -> tuple[_PackingRect, ...]:
    split: list[_PackingRect] = []
    for free in free_rectangles:
        if not _rectangles_intersect(free, used):
            split.append(free)
            continue
        if used.x > free.x + 1e-9:
            split.append(_PackingRect(free.x, free.y, used.x - free.x, free.height))
        if used.right < free.right - 1e-9:
            split.append(
                _PackingRect(used.right, free.y, free.right - used.right, free.height)
            )
        if used.y > free.y + 1e-9:
            split.append(_PackingRect(free.x, free.y, free.width, used.y - free.y))
        if used.bottom < free.bottom - 1e-9:
            split.append(
                _PackingRect(free.x, used.bottom, free.width, free.bottom - used.bottom)
            )
    return _prune_free_rectangles(split)


def _rectangles_intersect(first: _PackingRect, second: _PackingRect) -> bool:
    return not (
        first.right <= second.x + 1e-9
        or second.right <= first.x + 1e-9
        or first.bottom <= second.y + 1e-9
        or second.bottom <= first.y + 1e-9
    )


def _prune_free_rectangles(
    rectangles: list[_PackingRect],
) -> tuple[_PackingRect, ...]:
    # 同一または許容誤差内で相互包含する矩形は、先に出た一方だけを残す。
    useful = tuple(
        dict.fromkeys(
            rectangle
            for rectangle in rectangles
            if rectangle.width > 1e-9 and rectangle.height > 1e-9
        )
    )
    return tuple(
        rectangle
        for index, rectangle in enumerate(useful)
        if not any(
            index != other_index
            and _contains(other, rectangle)
            and (not _contains(rectangle, other) or other_index < index)
            for other_index, other in enumerate(useful)
        )
    )


def _contains(outer: _PackingRect, inner: _PackingRect) -> bool:
    return (
        inner.x >= outer.x - 1e-9
        and inner.y >= outer.y - 1e-9
        and inner.right <= outer.right + 1e-9
        and inner.bottom <= outer.bottom + 1e-9
    )


def _packing_score(
    placements: Mapping[int, PasteFlowCalibrationBounds], area: _PackingRect
) -> tuple[object, ...]:
    used_width = max(item.x + item.width for item in placements.values()) - area.x
    used_height = max(item.y + item.height for item in placements.values()) - area.y
    positions = tuple(
        (
            index,
            round(item.y, 9),
            round(item.x, 9),
        )
        for index, item in sorted(placements.items())
    )
    return used_width * used_height, used_height, used_width, positions


_LAYERS: tuple[tuple[PasteFlowCalibrationPreviewLayer, int], ...] = (
    ("F.Cu", pcbnew.F_Cu),
    ("F.Paste", pcbnew.F_Paste),
)


def _footprint_polygons(
    footprint: pcbnew.FOOTPRINT,
) -> tuple[PasteFlowCalibrationPolygon, ...]:
    polygons: list[PasteFlowCalibrationPolygon] = []
    for pad in footprint.Pads():
        for layer_name, layer in _LAYERS:
            if not pad.GetLayerSet().Contains(layer):
                continue
            shape = effective_pad_polygon(pad, layer)
            for index in range(shape.OutlineCount()):
                points = tuple(
                    PasteFlowCalibrationPoint(to_mm(point.x), to_mm(point.y))
                    for point in shape.Outline(index).CPoints()
                )
                if len(points) >= 3:
                    polygons.append(PasteFlowCalibrationPolygon(layer_name, points))
    return tuple(polygons)


def _rectangle_points(
    bounds: PasteFlowCalibrationBounds,
) -> tuple[PasteFlowCalibrationPoint, ...]:
    return (
        PasteFlowCalibrationPoint(bounds.x, bounds.y),
        PasteFlowCalibrationPoint(bounds.x + bounds.width, bounds.y),
        PasteFlowCalibrationPoint(bounds.x + bounds.width, bounds.y + bounds.height),
        PasteFlowCalibrationPoint(bounds.x, bounds.y + bounds.height),
    )
