"""設定解決、preview、KiCad基板生成を束ねる公開facade."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Mapping
from pathlib import Path
from threading import Lock

import attrs
import pcbnew

from pcbasm.pcb.footprint import FootprintInfo, duplicate_footprint
from pcbasm.pcb.generate import generate_rect_pcb, save_board
from pcbasm.pcb.units import vector

from .catalog import PadCatalog, PadPattern, ResolvedPadPattern
from .config import (
    BoardConfig,
    CustomPadDraft,
    PatternSpec,
)
from .layout import BoardLayout, Rect, build_board_layout, preview_board_layout


@attrs.frozen
class ResolvedConfig:
    """正規化・実footprint解決済みの設定と表示カタログ."""

    config: BoardConfig
    catalog: tuple[PadPattern, ...]


@attrs.frozen
class BoardPreview:
    """同一解決planから得た設定、カタログ、preview layout."""

    config: BoardConfig
    catalog: tuple[PadPattern, ...]
    layout: BoardLayout
    overflow_message: str | None


@attrs.frozen
class PatternAddition:
    """footprintから未追加パターンだけを足した結果."""

    config: BoardConfig
    catalog: tuple[PadPattern, ...]
    added_count: int


@attrs.frozen
class _Plan:
    config: BoardConfig
    catalog: tuple[PadPattern, ...]
    resolved: Mapping[str, ResolvedPadPattern]


class BoardGenerator:
    """KiCad catalogを共有し、1回の解決planから各生成物を作る.

    配置可否を伴う ``layout`` / ``build_board`` / ``board_bytes`` は
    ``(成果物 | None, 超過理由 | None)`` を返す。
    """

    def __init__(self, footprint_root: Path | None = None) -> None:
        self._lock = Lock()
        self._pad_catalog = PadCatalog(footprint_root)

    @property
    def footprint_count(self) -> int:
        """検索対象となるKiCad footprint数."""

        with self._lock:
            return self._pad_catalog.footprint_count

    def search_footprints(
        self, query: str, limit: int = 30
    ) -> tuple[FootprintInfo, ...]:
        """library名とfootprint名を空白区切りのAND検索する."""

        with self._lock:
            return self._pad_catalog.search_footprints(query, limit)

    def pad_patterns_for(self, footprint_id: str) -> tuple[PadPattern, ...]:
        """footprint内の回転同値なパッドを1種類ずつ返す."""

        with self._lock:
            return self._pad_catalog.pad_patterns_for(footprint_id)

    def resolve_config(self, config: BoardConfig) -> ResolvedConfig:
        """設定を正規化し、全パターンを実形状へ解決する."""

        with self._lock:
            plan = self._resolve_plan(config)
            return ResolvedConfig(plan.config, plan.catalog)

    def preview(self, config: BoardConfig) -> BoardPreview:
        """1回の解決planから設定、カタログ、layoutを返す."""

        with self._lock:
            plan = self._resolve_plan(config)
            layout, overflow_message = preview_board_layout(plan.config, plan.resolved)
            return BoardPreview(
                config=plan.config,
                catalog=plan.catalog,
                layout=layout,
                overflow_message=overflow_message,
            )

    def add_footprint_patterns(
        self, config: BoardConfig, footprint_id: str
    ) -> PatternAddition:
        """footprint内の未追加パッド種を既定値付きで一括追加する."""

        with self._lock:
            candidates = self._pad_catalog.pad_patterns_for(footprint_id)
            existing_ids = {pattern.catalog_id for pattern in config.patterns}
            additions = tuple(
                PatternSpec(
                    catalog_id=item.catalog_id,
                    rotation_span_deg=item.default_rotation_span_deg,
                    rotation_count=item.default_rotation_count,
                    repeat_count=item.default_repeat_count,
                )
                for item in candidates
                if item.catalog_id not in existing_ids
            )
            updated = (
                attrs.evolve(config, patterns=(*config.patterns, *additions))
                if additions
                else config
            )
            plan = self._resolve_plan(updated)
            return PatternAddition(
                config=plan.config,
                catalog=plan.catalog,
                added_count=len(additions),
            )

    def add_custom_pad(
        self, config: BoardConfig, draft: CustomPadDraft
    ) -> ResolvedConfig:
        """任意寸法パッドを採番し、解決済み設定へ追加する."""

        with self._lock:
            custom_pad = self._pad_catalog.create_custom_pad(draft)
            plan = self._resolve_plan(
                attrs.evolve(
                    config,
                    custom_pads=(*config.custom_pads, custom_pad),
                    patterns=(
                        *config.patterns,
                        PatternSpec(catalog_id=custom_pad.catalog_id),
                    ),
                )
            )
            return ResolvedConfig(plan.config, plan.catalog)

    def layout(self, config: BoardConfig) -> tuple[BoardLayout | None, str | None]:
        """配置可能な設定の解決済みlayoutを返す（収まらなければ ``(None, 理由)``）."""

        with self._lock:
            plan = self._resolve_plan(config)
            return build_board_layout(plan.config, plan.resolved)

    def build_board(
        self, config: BoardConfig
    ) -> tuple[pcbnew.BOARD | None, str | None]:
        """解決済みlayoutと同じ位置へ単一パッドfootprintを置く（収まらなければ ``(None, 理由)``）."""

        with self._lock:
            return self._build_board(config)

    def board_bytes(self, config: BoardConfig) -> tuple[bytes | None, str | None]:
        """生成したKiCad基板をダウンロード可能なbytesで返す（収まらなければ ``(None, 理由)``）."""

        with self._lock:
            board, overflow_message = self._build_board(config)
            if board is None:
                return None, overflow_message
            with tempfile.TemporaryDirectory(prefix="pcbasm-paste-flow-board-") as temp:
                output = Path(temp) / "board.kicad_pcb"
                save_board(board, output)
                return output.read_bytes(), None

    def config_bytes(self, config: BoardConfig) -> bytes:
        """設定JSONをUTF-8 bytesで返す."""

        with self._lock:
            plan = self._resolve_plan(config)
            document = plan.config.to_document()
            return (
                json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
                + b"\n"
            )

    def _resolve_plan(self, config: BoardConfig) -> _Plan:
        normalized = config.normalized()
        custom_by_id = {item.catalog_id: item for item in normalized.custom_pads}
        resolved = {
            pattern.catalog_id: self._pad_catalog.resolve_pattern(
                pattern.catalog_id, custom_by_id
            )
            for pattern in normalized.patterns
        }
        catalog = tuple(
            resolved[pattern.catalog_id].item for pattern in normalized.patterns
        )
        return _Plan(normalized, catalog, resolved)

    def _build_board(
        self, config: BoardConfig
    ) -> tuple[pcbnew.BOARD | None, str | None]:
        plan = self._resolve_plan(config)
        layout, overflow_message = build_board_layout(plan.config, plan.resolved)
        if layout is None:
            return None, overflow_message
        board = generate_rect_pcb(layout.board.width_mm, layout.board.height_mm)
        _add_purge_pad(board, layout.purge_pad)
        _add_flow_pads(board, layout.flow_pads)
        for pad_layout in layout.pads:
            resolved = plan.resolved[pad_layout.catalog_id]
            footprint = duplicate_footprint(resolved.template)
            footprint.SetReference(pad_layout.reference)
            footprint.SetValue(pad_layout.display_name)
            footprint.SetPosition(vector(pad_layout.x, pad_layout.y))
            footprint.SetOrientationDegrees(pad_layout.rotation_deg)
            footprint.Reference().SetVisible(False)
            footprint.Value().SetVisible(False)
            board.Add(footprint)
        return board, None


def _add_flow_pads(board: pcbnew.BOARD, rects: tuple[Rect, ...]) -> None:
    """流量計測用の正方形パッドを ``FLOW1`` から採番して置く."""
    for index, bounds in enumerate(rects, start=1):
        _add_square_pad(
            board, bounds, reference=f"FLOW{index}", value="Paste flow gauge"
        )


def _add_purge_pad(board: pcbnew.BOARD, bounds: Rect) -> None:
    _add_square_pad(board, bounds, reference="PURGE", value="Paste purge")


def _add_square_pad(
    board: pcbnew.BOARD, bounds: Rect, *, reference: str, value: str
) -> None:
    center_x = bounds.x + bounds.width / 2.0
    center_y = bounds.y + bounds.height / 2.0
    footprint = pcbnew.FOOTPRINT(board)
    footprint.SetReference(reference)
    footprint.SetValue(value)
    footprint.SetPosition(vector(center_x, center_y))
    footprint.Reference().SetVisible(False)
    footprint.Value().SetVisible(False)
    pad = pcbnew.PAD(footprint)
    pad.SetNumber("1")
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    pad.SetShape(pcbnew.PAD_SHAPE_RECTANGLE)
    pad.SetSize(vector(bounds.width, bounds.height))
    pad.SetPosition(vector(center_x, center_y))
    pad.SetLayerSet(pad.SMDMask())
    footprint.Add(pad)
    board.Add(footprint)
