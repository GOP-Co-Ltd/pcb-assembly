"""設定解決、preview、KiCad基板生成を束ねる公開facade."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Mapping
from pathlib import Path
from threading import Lock

import attrs
import pcbnew

from pcbasm.pcb.generate import generate_rect_pcb, save_board

from .catalog import (
    PasteFlowCalibrationFootprintInfo,
    PasteFlowCalibrationPadCatalog,
    PasteFlowCalibrationPadPattern,
    PasteFlowCalibrationResolvedPadPattern,
    duplicate_footprint,
    vector,
)
from .config import (
    PasteFlowCalibrationBoardConfig,
    PasteFlowCalibrationCustomPadDraft,
    PasteFlowCalibrationPattern,
    normalize_paste_flow_calibration_board_config,
    normalized_paste_flow_calibration_board_document,
)
from .layout import (
    PasteFlowCalibrationBoardLayout,
    PasteFlowCalibrationBounds,
    build_paste_flow_calibration_board_layout,
)


@attrs.frozen
class PasteFlowCalibrationResolvedConfig:
    """正規化・実footprint解決済みの設定と表示カタログ."""

    config: PasteFlowCalibrationBoardConfig
    catalog: tuple[PasteFlowCalibrationPadPattern, ...]


@attrs.frozen
class PasteFlowCalibrationBoardPreview:
    """同一解決planから得た設定、カタログ、preview layout."""

    config: PasteFlowCalibrationBoardConfig
    catalog: tuple[PasteFlowCalibrationPadPattern, ...]
    layout: PasteFlowCalibrationBoardLayout


@attrs.frozen
class PasteFlowCalibrationPatternAddition:
    """footprintから未追加パターンだけを足した結果."""

    config: PasteFlowCalibrationBoardConfig
    catalog: tuple[PasteFlowCalibrationPadPattern, ...]
    added_count: int


@attrs.frozen
class _PasteFlowCalibrationPlan:
    config: PasteFlowCalibrationBoardConfig
    catalog: tuple[PasteFlowCalibrationPadPattern, ...]
    resolved: Mapping[str, PasteFlowCalibrationResolvedPadPattern]


class PasteFlowCalibrationBoardGenerator:
    """KiCad catalogを共有し、1回の解決planから各生成物を作る."""

    def __init__(self, footprint_root: Path | None = None) -> None:
        self._lock = Lock()
        self._pad_catalog = PasteFlowCalibrationPadCatalog(footprint_root)

    @property
    def footprint_count(self) -> int:
        """検索対象となるKiCad footprint数."""

        with self._lock:
            return self._pad_catalog.footprint_count

    def search_footprints(
        self, query: str, limit: int = 30
    ) -> tuple[PasteFlowCalibrationFootprintInfo, ...]:
        """library名とfootprint名を空白区切りのAND検索する."""

        with self._lock:
            return self._pad_catalog.search_footprints(query, limit)

    def pad_patterns_for(
        self, footprint_id: str
    ) -> tuple[PasteFlowCalibrationPadPattern, ...]:
        """footprint内の回転同値なパッドを1種類ずつ返す."""

        with self._lock:
            return self._pad_catalog.pad_patterns_for(footprint_id)

    def resolve_config(
        self, config: PasteFlowCalibrationBoardConfig
    ) -> PasteFlowCalibrationResolvedConfig:
        """設定を正規化し、全パターンを実形状へ解決する."""

        with self._lock:
            plan = self._resolve_plan(config)
            return PasteFlowCalibrationResolvedConfig(plan.config, plan.catalog)

    def preview(
        self, config: PasteFlowCalibrationBoardConfig
    ) -> PasteFlowCalibrationBoardPreview:
        """1回の解決planから設定、カタログ、layoutを返す."""

        with self._lock:
            plan = self._resolve_plan(config)
            layout = build_paste_flow_calibration_board_layout(
                plan.config, plan.resolved
            )
            return PasteFlowCalibrationBoardPreview(
                config=plan.config,
                catalog=plan.catalog,
                layout=layout,
            )

    def add_footprint_patterns(
        self,
        config: PasteFlowCalibrationBoardConfig,
        footprint_id: str,
    ) -> PasteFlowCalibrationPatternAddition:
        """footprint内の未追加パッド種を既定値付きで一括追加する."""

        with self._lock:
            candidates = self._pad_catalog.pad_patterns_for(footprint_id)
            existing_ids = {pattern.catalog_id for pattern in config.patterns}
            additions = tuple(
                PasteFlowCalibrationPattern(
                    catalog_id=item.catalog_id,
                    rotation_span_deg=item.default_rotation_span_deg,
                    rotation_count=item.default_rotation_count,
                    repeat_count=item.default_repeat_count,
                    transpose=item.default_transpose,
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
            return PasteFlowCalibrationPatternAddition(
                config=plan.config,
                catalog=plan.catalog,
                added_count=len(additions),
            )

    def add_custom_pad(
        self,
        config: PasteFlowCalibrationBoardConfig,
        draft: PasteFlowCalibrationCustomPadDraft,
    ) -> PasteFlowCalibrationResolvedConfig:
        """任意寸法パッドを採番し、解決済み設定へ追加する."""

        with self._lock:
            custom_pad = self._pad_catalog.create_custom_pad(draft)
            plan = self._resolve_plan(
                attrs.evolve(
                    config,
                    custom_pads=(*config.custom_pads, custom_pad),
                    patterns=(
                        *config.patterns,
                        PasteFlowCalibrationPattern(catalog_id=custom_pad.catalog_id),
                    ),
                )
            )
            return PasteFlowCalibrationResolvedConfig(plan.config, plan.catalog)

    def layout(
        self, config: PasteFlowCalibrationBoardConfig
    ) -> PasteFlowCalibrationBoardLayout:
        """解決済みpreview layoutだけを返す."""

        return self.preview(config).layout

    def build_board(self, config: PasteFlowCalibrationBoardConfig) -> pcbnew.BOARD:
        """解決済みlayoutと同じ位置へ単一パッドfootprintを置く."""

        with self._lock:
            plan = self._resolve_plan(config)
            layout = build_paste_flow_calibration_board_layout(
                plan.config, plan.resolved
            )
            return self._build_board(plan, layout)

    def board_bytes(self, config: PasteFlowCalibrationBoardConfig) -> bytes:
        """生成したKiCad基板をダウンロード可能なbytesで返す."""

        with self._lock:
            plan = self._resolve_plan(config)
            layout = build_paste_flow_calibration_board_layout(
                plan.config, plan.resolved
            )
            board = self._build_board(plan, layout)
            with tempfile.TemporaryDirectory(prefix="pcbasm-paste-flow-board-") as temp:
                output = Path(temp) / "board.kicad_pcb"
                save_board(board, output)
                return output.read_bytes()

    def config_bytes(self, config: PasteFlowCalibrationBoardConfig) -> bytes:
        """設定JSONをUTF-8 bytesで返す."""

        with self._lock:
            plan = self._resolve_plan(config)
            document = normalized_paste_flow_calibration_board_document(plan.config)
            return (
                json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
                + b"\n"
            )

    def _resolve_plan(
        self, config: PasteFlowCalibrationBoardConfig
    ) -> _PasteFlowCalibrationPlan:
        normalized = normalize_paste_flow_calibration_board_config(config)
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
        return _PasteFlowCalibrationPlan(normalized, catalog, resolved)

    @staticmethod
    def _build_board(
        plan: _PasteFlowCalibrationPlan,
        layout: PasteFlowCalibrationBoardLayout,
    ) -> pcbnew.BOARD:
        board = generate_rect_pcb(layout.board.width_mm, layout.board.height_mm)
        _add_purge_pad(board, layout.purge_pad)
        for group in layout.groups:
            resolved = plan.resolved[group.catalog_id]
            for pad_layout in group.pads:
                footprint = duplicate_footprint(resolved.template)
                footprint.SetReference(pad_layout.reference)
                footprint.SetValue(
                    f"{resolved.item.footprint_label} / {resolved.item.label}"
                )
                footprint.SetPosition(vector(pad_layout.x, pad_layout.y))
                footprint.SetOrientationDegrees(pad_layout.rotation_deg)
                footprint.Reference().SetVisible(False)
                footprint.Value().SetVisible(False)
                board.Add(footprint)
        return board


def _add_purge_pad(board: pcbnew.BOARD, bounds: PasteFlowCalibrationBounds) -> None:
    center_x = bounds.x + bounds.width / 2.0
    center_y = bounds.y + bounds.height / 2.0
    footprint = pcbnew.FOOTPRINT(board)
    footprint.SetReference("PURGE1")
    footprint.SetValue("Paste purge")
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
