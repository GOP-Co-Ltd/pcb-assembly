"""解決済みpreview DTOとパッド単位の自動最適配置."""

from __future__ import annotations

from collections.abc import Mapping

import attrs
import pcbnew

from pcbasm.geometry.packing import Rect, pack_rects
from pcbasm.pcb.footprint import (
    FootprintEnvelope,
    duplicate_footprint,
    footprint_envelope,
    footprint_polygons,
)
from pcbasm.pcb.units import vector

from .catalog import PasteFlowCalibrationResolvedPadPattern
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
    packed = _pack(config, pads, area)
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
    packed = _pack(config, pads, area)
    if packed is not None:
        return packed
    return _stack_pads_for_overflow_preview(config, pads)


def _pack(
    config: PasteFlowCalibrationBoardConfig,
    pads: tuple[_PadToPack, ...],
    area: Rect,
) -> dict[int, PasteFlowCalibrationBounds] | None:
    placed = pack_rects(
        [(pad.width, pad.height) for pad in pads],
        area,
        keepouts=(_purge_keepout(config),),
        gap=config.board.pad_gap_mm,
    )
    if placed is None:
        return None
    return {
        pad.index: PasteFlowCalibrationBounds(rect.x, rect.y, rect.width, rect.height)
        for pad, rect in zip(pads, placed, strict=True)
    }


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


def _packing_area(config: PasteFlowCalibrationBoardConfig) -> Rect:
    board = config.board
    return Rect(
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


def _purge_keepout(config: PasteFlowCalibrationBoardConfig) -> Rect:
    area = _packing_area(config)
    return Rect(
        x=area.x,
        y=area.y,
        width=config.purge_pad.width_mm + config.board.pad_gap_mm,
        height=config.purge_pad.height_mm + config.board.pad_gap_mm,
    )


def _overflow_preview_area(
    config: PasteFlowCalibrationBoardConfig,
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


_LAYERS: tuple[tuple[PasteFlowCalibrationPreviewLayer, int], ...] = (
    ("F.Cu", pcbnew.F_Cu),
    ("F.Paste", pcbnew.F_Paste),
)


def _footprint_polygons(
    footprint: pcbnew.FOOTPRINT,
) -> tuple[PasteFlowCalibrationPolygon, ...]:
    return tuple(
        PasteFlowCalibrationPolygon(
            layer_name,
            tuple(PasteFlowCalibrationPoint(p.x, p.y) for p in points),
        )
        for layer_name, points in footprint_polygons(footprint, _LAYERS)
    )


def _rectangle_points(
    bounds: PasteFlowCalibrationBounds,
) -> tuple[PasteFlowCalibrationPoint, ...]:
    return (
        PasteFlowCalibrationPoint(bounds.x, bounds.y),
        PasteFlowCalibrationPoint(bounds.x + bounds.width, bounds.y),
        PasteFlowCalibrationPoint(bounds.x + bounds.width, bounds.y + bounds.height),
        PasteFlowCalibrationPoint(bounds.x, bounds.y + bounds.height),
    )
