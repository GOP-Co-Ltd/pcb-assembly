"""解決済み preview DTO とパッド単位の自動最適配置."""

from __future__ import annotations

from collections.abc import Mapping

import attrs
import pcbnew

from pcbasm.geometry import Point2d
from pcbasm.geometry.packing import Rect, pack_rects
from pcbasm.pcb.footprint import (
    FootprintEnvelope,
    duplicate_footprint,
    footprint_polygons,
)
from pcbasm.pcb.units import vector

from .catalog import ResolvedPadPattern
from .config import BoardConfig, BoardSpec, PreviewLayer

# 「ちょうど収まる」寸法が浮動小数点誤差で落ちないための微小許容
_EPSILON = 1e-9


@attrs.frozen
class LayerPolygon:
    """Preview 用の解決済みパッドポリゴン（基板左上原点 [mm]）."""

    layer: PreviewLayer
    points: tuple[Point2d, ...]


@attrs.frozen
class PadLayout:
    """生成する単一パッド footprint の解決済み配置."""

    catalog_id: str
    display_name: str
    reference: str
    bounds: Rect
    x: float
    y: float
    rotation_deg: float
    polygons: tuple[LayerPolygon, ...]


@attrs.frozen
class PatternLayout:
    """パッド設定ごとの解決済み回転角."""

    catalog_id: str
    angles_deg: tuple[float, ...]


@attrs.frozen
class BoardLayout:
    """WebUI と KiCad 生成が共有する完全に解決済みの配置."""

    board: BoardSpec
    placement_area: Rect
    preview_bounds: Rect
    purge_pad: Rect
    purge_polygons: tuple[LayerPolygon, ...]
    flow_pads: tuple[Rect, ...]
    flow_polygons: tuple[tuple[LayerPolygon, ...], ...]
    patterns: tuple[PatternLayout, ...]
    pads: tuple[PadLayout, ...]

    @property
    def pad_count(self) -> int:
        return len(self.pads)


@attrs.frozen
class _PadToPack:
    index: int
    catalog_id: str
    display_name: str
    rotation_deg: float
    envelope: FootprintEnvelope

    @property
    def width(self) -> float:
        return self.envelope.width

    @property
    def height(self) -> float:
        return self.envelope.height


def build_board_layout(
    config: BoardConfig,
    resolved: Mapping[str, ResolvedPadPattern],
) -> tuple[BoardLayout | None, str | None]:
    """解決済み config と template から配置可能な layout を構築する.

    Returns:
        ``(layout, None)`` または、基板に収まらないときは ``(None, 理由)``
    """

    patterns, pads = _pads_to_pack(config, resolved)
    placements, overflow_message = _pack_pads(config, pads)
    if placements is None:
        return None, overflow_message
    return _build_layout(config, resolved, patterns, pads, placements), None


def preview_board_layout(
    config: BoardConfig,
    resolved: Mapping[str, ResolvedPadPattern],
) -> tuple[BoardLayout, str | None]:
    """超過時も全パッドを含む診断用 layout と理由を返す."""

    patterns, pads = _pads_to_pack(config, resolved)
    placements, overflow_message = _pack_pads(config, pads)
    if placements is None:
        placements = _pack_pads_for_overflow_preview(config, pads)
    return (
        _build_layout(config, resolved, patterns, pads, placements),
        overflow_message,
    )


def _pads_to_pack(
    config: BoardConfig,
    resolved: Mapping[str, ResolvedPadPattern],
) -> tuple[tuple[PatternLayout, ...], tuple[_PadToPack, ...]]:
    layouts: list[PatternLayout] = []
    pads: list[_PadToPack] = []
    for pattern in config.patterns:
        resolved_pattern = resolved[pattern.catalog_id]
        angles = tuple(
            index * pattern.rotation_span_deg / pattern.rotation_count
            for index in range(pattern.rotation_count)
        )
        envelopes = tuple(
            FootprintEnvelope.measure(resolved_pattern.template, angle)
            for angle in angles
        )
        layouts.append(PatternLayout(pattern.catalog_id, angles))
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
    config: BoardConfig,
    resolved: Mapping[str, ResolvedPadPattern],
    patterns: tuple[PatternLayout, ...],
    pads: tuple[_PadToPack, ...],
    placements: Mapping[int, Rect],
) -> BoardLayout:
    pad_layouts: list[PadLayout] = []
    for pad in pads:
        bounds = placements[pad.index]
        anchor_x = bounds.x - pad.envelope.min_x
        anchor_y = bounds.y - pad.envelope.min_y
        placed = duplicate_footprint(resolved[pad.catalog_id].template)
        placed.SetPosition(vector(anchor_x, anchor_y))
        placed.SetOrientationDegrees(pad.rotation_deg)
        pad_layouts.append(
            PadLayout(
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
    purge = _purge_rect(config)
    purge_points = _rectangle_points(purge)
    flow_pads = _flow_pad_rects(config) or ()
    flow_polygons = tuple(
        (LayerPolygon("F.Cu", points), LayerPolygon("F.Paste", points))
        for points in (_rectangle_points(rect) for rect in flow_pads)
    )
    return BoardLayout(
        board=config.board,
        placement_area=_packing_area(config),
        preview_bounds=_preview_bounds(config.board, purge, flow_pads, pad_layouts),
        purge_pad=purge,
        purge_polygons=(
            LayerPolygon("F.Cu", purge_points),
            LayerPolygon("F.Paste", purge_points),
        ),
        flow_pads=flow_pads,
        flow_polygons=flow_polygons,
        patterns=patterns,
        pads=tuple(pad_layouts),
    )


def _preview_bounds(
    board: BoardSpec,
    purge: Rect,
    flow_pads: tuple[Rect, ...],
    pads: list[PadLayout],
) -> Rect:
    bounds = (
        Rect(0.0, 0.0, board.width_mm, board.height_mm),
        purge,
        *flow_pads,
        *(pad.bounds for pad in pads),
    )
    left = min(item.x for item in bounds)
    top = min(item.y for item in bounds)
    right = max(item.right for item in bounds)
    bottom = max(item.bottom for item in bounds)
    return Rect(left, top, right - left, bottom - top)


def _pack_pads(
    config: BoardConfig,
    pads: tuple[_PadToPack, ...],
) -> tuple[dict[int, Rect] | None, str | None]:
    """配置領域へ全パッドを詰める。収まらなければ ``(None, 理由)``."""
    if (message := _validate_purge_region(config)) is not None:
        return None, message
    if _flow_pad_rects(config) is None:
        flow = config.flow_pads
        return None, (
            f"流量計測パッド{flow.count}個（{flow.size_mm:.2f} mm角）が"
            "パージ領域の右と下の帯に収まりません"
        )
    area = _packing_area(config)
    for pad in pads:
        if pad.width > area.width + 1e-9 or pad.height > area.height + 1e-9:
            return None, (
                f"{pad.display_name}のパッド（{pad.width:.2f} × {pad.height:.2f} mm）が"
                f"配置領域{area.width:.2f} × {area.height:.2f} mmに収まりません"
            )
    packed = _pack(config, pads, area)
    if packed is None:
        return None, "自動最適配置でもすべてのパッドが基板の配置可能領域に収まりません"
    return packed, None


def _pack_pads_for_overflow_preview(
    config: BoardConfig,
    pads: tuple[_PadToPack, ...],
) -> dict[int, Rect]:
    area = _overflow_preview_area(config, pads)
    packed = _pack(config, pads, area)
    if packed is not None:
        return packed
    return _stack_pads_for_overflow_preview(config, pads)


def _pack(
    config: BoardConfig,
    pads: tuple[_PadToPack, ...],
    area: Rect,
) -> dict[int, Rect] | None:
    placed = pack_rects(
        [(pad.width, pad.height) for pad in pads],
        area,
        keepouts=_keepouts(config),
        gap=config.board.pad_gap_mm,
    )
    if placed is None:
        return None
    return {pad.index: rect for pad, rect in zip(pads, placed, strict=True)}


def _validate_purge_region(config: BoardConfig) -> str | None:
    board = config.board
    available_width = board.width_mm - 2 * board.edge_margin_mm
    available_height = board.height_mm - 2 * board.edge_margin_mm
    if config.purge_pad.width_mm > available_width + 1e-9:
        return (
            f"purge pad幅{config.purge_pad.width_mm:.2f} mmが"
            f"配置可能幅{available_width:.2f} mmを超えます"
        )
    if config.purge_pad.height_mm > available_height + 1e-9:
        return (
            f"purge pad高さ{config.purge_pad.height_mm:.2f} mmが"
            f"配置可能高さ{available_height:.2f} mmを超えます"
        )
    return None


def _packing_area(config: BoardConfig) -> Rect:
    board = config.board
    return Rect(
        x=board.edge_margin_mm,
        y=board.edge_margin_mm,
        width=board.width_mm - 2 * board.edge_margin_mm,
        height=board.height_mm - 2 * board.edge_margin_mm,
    )


def _purge_rect(config: BoardConfig) -> Rect:
    """パージ領域（有効領域の左上）."""
    area = _packing_area(config)
    return Rect(
        x=area.x,
        y=area.y,
        width=config.purge_pad.width_mm,
        height=config.purge_pad.height_mm,
    )


def _flow_pad_rects(config: BoardConfig) -> tuple[Rect, ...] | None:
    """流量計測パッドをパージ領域の右から並べる（収まらなければ ``None``）.

    1 行目はパージ領域の右、以降は行を下へ折り返す。折り返し後も収まらない場合は
    ``None`` を返し、呼び出し側が理由を組み立てる。
    """
    flow = config.flow_pads
    if flow.count == 0:
        return ()
    area = _packing_area(config)
    gap = config.board.pad_gap_mm
    size = flow.size_mm
    purge = _purge_rect(config)
    rects: list[Rect] = []
    x = purge.right + gap
    y = area.y
    row_bottom = max(purge.bottom, y + size)
    for _ in range(flow.count):
        if x + size > area.right + _EPSILON:
            y = row_bottom + gap
            x = area.x
            row_bottom = y + size
        if x + size > area.right + _EPSILON or y + size > area.bottom + _EPSILON:
            return None
        rects.append(Rect(x=x, y=y, width=size, height=size))
        x += size + gap
    return tuple(rects)


def _keepouts(config: BoardConfig) -> tuple[Rect, ...]:
    """通常パッドの配置禁止領域（パージ領域と流量計測パッド）."""
    gap = config.board.pad_gap_mm
    return (
        _purge_keepout(config),
        *(
            Rect(
                x=rect.x - gap,
                y=rect.y - gap,
                width=rect.width + 2.0 * gap,
                height=rect.height + 2.0 * gap,
            )
            for rect in _flow_pad_rects(config) or ()
        ),
    )


def _purge_keepout(config: BoardConfig) -> Rect:
    area = _packing_area(config)
    return Rect(
        x=area.x,
        y=area.y,
        width=config.purge_pad.width_mm + config.board.pad_gap_mm,
        height=config.purge_pad.height_mm + config.board.pad_gap_mm,
    )


def _overflow_preview_area(
    config: BoardConfig,
    pads: tuple[_PadToPack, ...],
) -> Rect:
    area = _packing_area(config)
    purge_keepout = _purge_keepout(config)
    width = max(area.width, purge_keepout.width, *(pad.width for pad in pads))
    stacked_height = purge_keepout.height + sum(
        pad.height + config.board.pad_gap_mm for pad in pads
    )
    return Rect(
        x=area.x,
        y=area.y,
        width=width,
        height=max(area.height, stacked_height),
    )


def _stack_pads_for_overflow_preview(
    config: BoardConfig,
    pads: tuple[_PadToPack, ...],
) -> dict[int, Rect]:
    area = _packing_area(config)
    y = max(area.y, _purge_keepout(config).bottom)
    placements: dict[int, Rect] = {}
    for pad in pads:
        placements[pad.index] = Rect(x=area.x, y=y, width=pad.width, height=pad.height)
        y += pad.height + config.board.pad_gap_mm
    return placements


_LAYERS: tuple[tuple[PreviewLayer, int], ...] = (
    ("F.Cu", pcbnew.F_Cu),
    ("F.Paste", pcbnew.F_Paste),
)


def _footprint_polygons(footprint: pcbnew.FOOTPRINT) -> tuple[LayerPolygon, ...]:
    return tuple(
        LayerPolygon(layer_name, tuple(Point2d(p.x, p.y) for p in points))
        for layer_name, points in footprint_polygons(footprint, _LAYERS)
    )


def _rectangle_points(bounds: Rect) -> tuple[Point2d, ...]:
    return (
        Point2d(bounds.x, bounds.y),
        Point2d(bounds.right, bounds.y),
        Point2d(bounds.right, bounds.bottom),
        Point2d(bounds.x, bounds.bottom),
    )
