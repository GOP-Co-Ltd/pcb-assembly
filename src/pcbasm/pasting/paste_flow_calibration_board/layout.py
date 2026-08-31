"""解決済みpreview DTOとパッドグループの配置."""

from __future__ import annotations

import math
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
    PasteFlowCalibrationPattern,
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
    reference: str
    x: float
    y: float
    rotation_deg: float
    polygons: tuple[PasteFlowCalibrationPolygon, ...]


@attrs.frozen
class PasteFlowCalibrationGroupLayout:
    """回転・繰り返しの解決済み矩形グループ."""

    catalog_id: str
    label: str
    footprint_label: str
    family_id: str
    family_label: str
    bounds: PasteFlowCalibrationBounds
    cell_width_mm: float
    cell_height_mm: float
    angles_deg: tuple[float, ...]
    repeat_count: int
    transpose: bool
    pads: tuple[PasteFlowCalibrationPadLayout, ...]


@attrs.frozen
class PasteFlowCalibrationBoardLayout:
    """WebUIとKiCad生成が共有する完全に解決済みの配置."""

    board: PasteFlowCalibrationBoardSpec
    placement_area: PasteFlowCalibrationBounds
    preview_bounds: PasteFlowCalibrationBounds
    purge_pad: PasteFlowCalibrationBounds
    purge_polygons: tuple[PasteFlowCalibrationPolygon, ...]
    groups: tuple[PasteFlowCalibrationGroupLayout, ...]
    pad_count: int


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
class _PackedGroup:
    bounds: PasteFlowCalibrationBounds
    transpose: bool


_GroupMetrics: TypeAlias = tuple[
    float,
    float,
    tuple[float, ...],
    tuple[PasteFlowCalibrationFootprintEnvelope, ...],
]
_PackingVariant: TypeAlias = tuple[bool, float, float]
_PackingHeuristic: TypeAlias = Literal["short_side", "area", "bottom_left"]


def build_paste_flow_calibration_board_layout(
    config: PasteFlowCalibrationBoardConfig,
    resolved: Mapping[str, PasteFlowCalibrationResolvedPadPattern],
) -> PasteFlowCalibrationBoardLayout:
    """解決済みconfigとtemplateから配置可能なlayoutを構築する."""

    metrics = _group_metrics_by_catalog_id(config, resolved)
    placements = _pack_groups(config, metrics, resolved)
    return _build_layout(config, resolved, metrics, placements)


def preview_paste_flow_calibration_board_layout(
    config: PasteFlowCalibrationBoardConfig,
    resolved: Mapping[str, PasteFlowCalibrationResolvedPadPattern],
) -> tuple[PasteFlowCalibrationBoardLayout, str | None]:
    """超過時も全パターンを含む診断用layoutと理由を返す."""

    metrics = _group_metrics_by_catalog_id(config, resolved)
    try:
        placements = _pack_groups(config, metrics, resolved)
    except PasteFlowCalibrationBoardOverflowError as exc:
        placements = _pack_groups_for_overflow_preview(config, metrics, resolved)
        overflow_message: str | None = str(exc)
    else:
        overflow_message = None
    return (
        _build_layout(config, resolved, metrics, placements),
        overflow_message,
    )


def _group_metrics_by_catalog_id(
    config: PasteFlowCalibrationBoardConfig,
    resolved: Mapping[str, PasteFlowCalibrationResolvedPadPattern],
) -> dict[str, _GroupMetrics]:
    return {
        pattern.catalog_id: _group_metrics(
            pattern, resolved[pattern.catalog_id].template
        )
        for pattern in config.patterns
    }


def _build_layout(
    config: PasteFlowCalibrationBoardConfig,
    resolved: Mapping[str, PasteFlowCalibrationResolvedPadPattern],
    metrics: Mapping[str, _GroupMetrics],
    placements: Mapping[str, _PackedGroup],
) -> PasteFlowCalibrationBoardLayout:
    groups: list[PasteFlowCalibrationGroupLayout] = []
    pad_counter = 0
    for pattern in config.patterns:
        resolved_pattern = resolved[pattern.catalog_id]
        item = resolved_pattern.item
        packed = placements[pattern.catalog_id]
        bounds = packed.bounds
        transpose = packed.transpose
        cell_width, cell_height, angles, envelopes = metrics[pattern.catalog_id]
        pads: list[PasteFlowCalibrationPadLayout] = []
        for repeat_index in range(pattern.repeat_count):
            for rotation_index, angle in enumerate(angles):
                envelope = envelopes[rotation_index]
                column = repeat_index if transpose else rotation_index
                row = rotation_index if transpose else repeat_index
                cell_center_x = (
                    bounds.x
                    + column * (cell_width + config.board.pad_gap_mm)
                    + cell_width / 2.0
                )
                cell_center_y = (
                    bounds.y
                    + row * (cell_height + config.board.pad_gap_mm)
                    + cell_height / 2.0
                )
                anchor_x = cell_center_x - envelope.center_x
                anchor_y = cell_center_y - envelope.center_y
                pad_counter += 1
                reference = f"PAD{pad_counter}"
                placed = duplicate_footprint(resolved_pattern.template)
                placed.SetPosition(vector(anchor_x, anchor_y))
                placed.SetOrientationDegrees(angle)
                pads.append(
                    PasteFlowCalibrationPadLayout(
                        catalog_id=pattern.catalog_id,
                        reference=reference,
                        x=anchor_x,
                        y=anchor_y,
                        rotation_deg=angle,
                        polygons=_footprint_polygons(placed),
                    )
                )
        groups.append(
            PasteFlowCalibrationGroupLayout(
                catalog_id=pattern.catalog_id,
                label=item.label,
                footprint_label=item.footprint_label,
                family_id=item.family_id,
                family_label=item.family_label,
                bounds=bounds,
                cell_width_mm=cell_width,
                cell_height_mm=cell_height,
                angles_deg=angles,
                repeat_count=pattern.repeat_count,
                transpose=transpose,
                pads=tuple(pads),
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
    preview_bounds = _preview_bounds(config.board, purge, groups)
    return PasteFlowCalibrationBoardLayout(
        board=config.board,
        placement_area=placement_area,
        preview_bounds=preview_bounds,
        purge_pad=purge,
        purge_polygons=(
            PasteFlowCalibrationPolygon("F.Cu", purge_points),
            PasteFlowCalibrationPolygon("F.Paste", purge_points),
        ),
        groups=tuple(groups),
        pad_count=pad_counter,
    )


def _preview_bounds(
    board: PasteFlowCalibrationBoardSpec,
    purge: PasteFlowCalibrationBounds,
    groups: list[PasteFlowCalibrationGroupLayout],
) -> PasteFlowCalibrationBounds:
    bounds = (
        PasteFlowCalibrationBounds(0.0, 0.0, board.width_mm, board.height_mm),
        purge,
        *(group.bounds for group in groups),
    )
    left = min(item.x for item in bounds)
    top = min(item.y for item in bounds)
    right = max(item.x + item.width for item in bounds)
    bottom = max(item.y + item.height for item in bounds)
    return PasteFlowCalibrationBounds(left, top, right - left, bottom - top)


def _group_metrics(
    pattern: PasteFlowCalibrationPattern,
    footprint: pcbnew.FOOTPRINT,
) -> _GroupMetrics:
    angles = tuple(
        index * pattern.rotation_span_deg / pattern.rotation_count
        for index in range(pattern.rotation_count)
    )
    envelopes = tuple(footprint_envelope(footprint, angle) for angle in angles)
    cell_width = max(envelope.width for envelope in envelopes)
    cell_height = max(envelope.height for envelope in envelopes)
    return cell_width, cell_height, angles, envelopes


def _pack_groups(
    config: PasteFlowCalibrationBoardConfig,
    metrics: Mapping[str, _GroupMetrics],
    resolved: Mapping[str, PasteFlowCalibrationResolvedPadPattern],
) -> dict[str, _PackedGroup]:
    _validate_purge_region(config)
    if config.auto_pack:
        return _pack_groups_optimized(config, metrics, resolved)
    return _pack_groups_ordered(config, metrics, resolved)


def _pack_groups_for_overflow_preview(
    config: PasteFlowCalibrationBoardConfig,
    metrics: Mapping[str, _GroupMetrics],
    resolved: Mapping[str, PasteFlowCalibrationResolvedPadPattern],
) -> dict[str, _PackedGroup]:
    if not config.auto_pack:
        return _pack_groups_ordered(
            config,
            metrics,
            resolved,
            allow_overflow=True,
        )

    variants = {
        pattern.catalog_id: _packing_variants(
            pattern,
            metrics,
            config.board.pad_gap_mm,
        )
        for pattern in config.patterns
    }
    area = _overflow_preview_area(config, variants)
    packed = _find_optimized_packing(
        config.patterns,
        variants,
        area,
        _purge_keepout(config),
        config.board.pad_gap_mm,
    )
    if packed is not None:
        return packed
    return _pack_groups_ordered(
        config,
        metrics,
        resolved,
        allow_overflow=True,
    )


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


def _group_dimensions(
    pattern: PasteFlowCalibrationPattern,
    metrics: _GroupMetrics,
    gap: float,
    transpose: bool,
) -> tuple[float, float]:
    cell_width, cell_height, _angles, _envelopes = metrics
    column_count = pattern.repeat_count if transpose else pattern.rotation_count
    row_count = pattern.rotation_count if transpose else pattern.repeat_count
    return (
        column_count * cell_width + (column_count - 1) * gap,
        row_count * cell_height + (row_count - 1) * gap,
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


def _shelf_start_x(area: _PackingRect, purge_keepout: _PackingRect, y: float) -> float:
    if y < purge_keepout.bottom - 1e-9:
        return purge_keepout.right
    return area.x


def _pack_groups_ordered(
    config: PasteFlowCalibrationBoardConfig,
    metrics: Mapping[str, _GroupMetrics],
    resolved: Mapping[str, PasteFlowCalibrationResolvedPadPattern],
    *,
    allow_overflow: bool = False,
) -> dict[str, _PackedGroup]:
    board = config.board
    area = _packing_area(config)
    purge_keepout = _purge_keepout(config)
    right = area.right
    bottom = area.bottom
    x = purge_keepout.right
    y = area.y
    row_height = 0.0
    family_id: str | None = None
    placements: dict[str, _PackedGroup] = {}
    for pattern in config.patterns:
        item = resolved[pattern.catalog_id].item
        width, height = _group_dimensions(
            pattern,
            metrics[pattern.catalog_id],
            board.pad_gap_mm,
            pattern.transpose,
        )
        if not allow_overflow and width > area.width + 1e-9:
            raise PasteFlowCalibrationBoardOverflowError(
                f"{item.footprint_label} / {item.label}のグループ幅{width:.2f} mmが"
                f"配置可能幅{area.width:.2f} mmを超えます"
            )
        if family_id is not None and item.family_id != family_id:
            y += row_height + board.pad_gap_mm
            x = _shelf_start_x(area, purge_keepout, y)
            row_height = 0.0
        if x + width > right + 1e-9:
            if row_height > 0.0:
                y += row_height + board.pad_gap_mm
            elif y < purge_keepout.bottom - 1e-9:
                y = purge_keepout.bottom
            x = _shelf_start_x(area, purge_keepout, y)
            row_height = 0.0
        if x + width > right + 1e-9 and y < purge_keepout.bottom - 1e-9:
            y = purge_keepout.bottom
            x = area.x
        if not allow_overflow and y + height > bottom + 1e-9:
            raise PasteFlowCalibrationBoardOverflowError(
                f"{item.footprint_label} / {item.label}を配置すると基板高さを超えます"
                f"（必要下端{y + height:.2f} mm、配置可能下端{bottom:.2f} mm）"
            )
        placements[pattern.catalog_id] = _PackedGroup(
            bounds=PasteFlowCalibrationBounds(x=x, y=y, width=width, height=height),
            transpose=pattern.transpose,
        )
        x += width + board.pad_gap_mm
        row_height = max(row_height, height)
        family_id = item.family_id
    return placements


def _pack_groups_optimized(
    config: PasteFlowCalibrationBoardConfig,
    metrics: Mapping[str, _GroupMetrics],
    resolved: Mapping[str, PasteFlowCalibrationResolvedPadPattern],
) -> dict[str, _PackedGroup]:
    area = _packing_area(config)
    purge_keepout = _purge_keepout(config)
    variants = {
        pattern.catalog_id: _packing_variants(pattern, metrics, config.board.pad_gap_mm)
        for pattern in config.patterns
    }
    for pattern in config.patterns:
        if not any(
            width <= area.width + 1e-9 and height <= area.height + 1e-9
            for _transpose, width, height in variants[pattern.catalog_id]
        ):
            item = resolved[pattern.catalog_id].item
            raise PasteFlowCalibrationBoardOverflowError(
                f"{item.footprint_label} / {item.label}は転置しても"
                f"配置領域{area.width:.2f} × {area.height:.2f} mmに収まりません"
            )

    packed = _find_optimized_packing(
        config.patterns,
        variants,
        area,
        purge_keepout,
        config.board.pad_gap_mm,
    )
    if packed is None:
        raise PasteFlowCalibrationBoardOverflowError(
            "自動最適配置でもすべてのパッドグループを配置できず、"
            "基板の配置可能領域を超えます"
        )
    return packed


def _overflow_preview_area(
    config: PasteFlowCalibrationBoardConfig,
    variants: Mapping[str, tuple[_PackingVariant, ...]],
) -> _PackingRect:
    area = _packing_area(config)
    purge_keepout = _purge_keepout(config)
    width = max(
        area.width,
        purge_keepout.width,
        *(
            min(variant[1] for variant in variants[pattern.catalog_id])
            for pattern in config.patterns
        ),
    )
    stacked_height = purge_keepout.height + sum(
        min(
            variant[2]
            for variant in variants[pattern.catalog_id]
            if variant[1] <= width + 1e-9
        )
        + config.board.pad_gap_mm
        for pattern in config.patterns
    )
    return _PackingRect(
        x=area.x,
        y=area.y,
        width=width,
        height=max(area.height, stacked_height),
    )


def _find_optimized_packing(
    patterns: tuple[PasteFlowCalibrationPattern, ...],
    variants: Mapping[str, tuple[_PackingVariant, ...]],
    area: _PackingRect,
    purge_keepout: _PackingRect,
    gap: float,
) -> dict[str, _PackedGroup] | None:
    attempts: list[dict[str, _PackedGroup]] = []
    heuristics: tuple[_PackingHeuristic, ...] = (
        "short_side",
        "area",
        "bottom_left",
    )
    for order in _packing_orders(patterns, variants):
        for heuristic in heuristics:
            packed = _pack_max_rects(
                order,
                variants,
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


def _packing_variants(
    pattern: PasteFlowCalibrationPattern,
    metrics: Mapping[str, _GroupMetrics],
    gap: float,
) -> tuple[_PackingVariant, ...]:
    variants = tuple(
        (
            transpose,
            *_group_dimensions(pattern, metrics[pattern.catalog_id], gap, transpose),
        )
        for transpose in (False, True)
    )
    if math.isclose(variants[0][1], variants[1][1], abs_tol=1e-9) and math.isclose(
        variants[0][2], variants[1][2], abs_tol=1e-9
    ):
        return (variants[0],)
    return variants


def _packing_orders(
    patterns: tuple[PasteFlowCalibrationPattern, ...],
    variants: Mapping[str, tuple[_PackingVariant, ...]],
) -> tuple[tuple[PasteFlowCalibrationPattern, ...], ...]:
    def maximum(catalog_id: str, value: int) -> float:
        return max(item[value] for item in variants[catalog_id])

    sort_keys = (
        lambda pattern: (
            -maximum(pattern.catalog_id, 1) * maximum(pattern.catalog_id, 2),
        ),
        lambda pattern: (
            -max(maximum(pattern.catalog_id, 1), maximum(pattern.catalog_id, 2)),
        ),
        lambda pattern: (-maximum(pattern.catalog_id, 1),),
        lambda pattern: (-maximum(pattern.catalog_id, 2),),
        lambda pattern: (
            -(maximum(pattern.catalog_id, 1) + maximum(pattern.catalog_id, 2)),
        ),
    )
    orders = [patterns]
    seen = {tuple(pattern.catalog_id for pattern in patterns)}
    for key in sort_keys:
        order = tuple(sorted(patterns, key=key))
        identity = tuple(pattern.catalog_id for pattern in order)
        if identity not in seen:
            orders.append(order)
            seen.add(identity)
    return tuple(orders)


def _pack_max_rects(
    patterns: tuple[PasteFlowCalibrationPattern, ...],
    variants: Mapping[str, tuple[_PackingVariant, ...]],
    area: _PackingRect,
    purge_keepout: _PackingRect,
    gap: float,
    heuristic: _PackingHeuristic,
) -> dict[str, _PackedGroup] | None:
    free_rectangles = _split_free_rectangles(
        (_PackingRect(area.x, area.y, area.width + gap, area.height + gap),),
        purge_keepout,
    )
    placements: dict[str, _PackedGroup] = {}
    for pattern in patterns:
        choices: list[tuple[tuple[float, ...], _PackingRect, bool, float, float]] = []
        for transpose, width, height in variants[pattern.catalog_id]:
            packed_width = width + gap
            packed_height = height + gap
            for free in free_rectangles:
                if (
                    packed_width > free.width + 1e-9
                    or packed_height > free.height + 1e-9
                ):
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
                            transpose,
                        ),
                        free,
                        transpose,
                        width,
                        height,
                    )
                )
        if not choices:
            return None
        _score, free, transpose, width, height = min(choices, key=lambda item: item[0])
        used = _PackingRect(free.x, free.y, width + gap, height + gap)
        placements[pattern.catalog_id] = _PackedGroup(
            PasteFlowCalibrationBounds(free.x, free.y, width, height), transpose
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
    transpose: bool,
) -> tuple[float, ...]:
    short_side = min(remaining_width, remaining_height)
    long_side = max(remaining_width, remaining_height)
    area_waste = free.width * free.height - width * height
    suffix = (free.y, free.x, float(transpose))
    if heuristic == "area":
        return (area_waste, short_side, long_side, *suffix)
    if heuristic == "bottom_left":
        return (free.y + height, free.x, short_side, long_side, float(transpose))
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
    placements: Mapping[str, _PackedGroup], area: _PackingRect
) -> tuple[object, ...]:
    used_width = (
        max(item.bounds.x + item.bounds.width for item in placements.values()) - area.x
    )
    used_height = (
        max(item.bounds.y + item.bounds.height for item in placements.values()) - area.y
    )
    transpose_count = sum(item.transpose for item in placements.values())
    positions = tuple(
        sorted(
            (
                catalog_id,
                round(item.bounds.y, 9),
                round(item.bounds.x, 9),
                item.transpose,
            )
            for catalog_id, item in placements.items()
        )
    )
    return used_width * used_height, used_height, used_width, transpose_count, positions


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
