"""はんだペースト流量キャリブレーション基板の配置・生成.

KiCad footprint内の同一形状パッドを1つのパッドパターンとして抽出し、回転列と
繰り返し行からなる規則的なグループへ配置する。WebUIはこのモジュールが返す 解決済みlayoutを描画し、配置規則やパッド分類を持たない。
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import attrs
import pcbnew

from pcbasm.pcb.generate import generate_rect_pcb, save_board

PASTE_FLOW_CALIBRATION_BOARD_KIND = "paste_flow_calibration_board"
PASTE_FLOW_CALIBRATION_BOARD_SCHEMA_VERSION = 1
DEFAULT_KICAD9_FOOTPRINT_DIR = Path("/usr/share/kicad/footprints")


class PasteFlowCalibrationBoardEnvironmentError(RuntimeError):
    """KiCad footprint 環境が基板生成に使えない."""


class PasteFlowCalibrationBoardConfigError(ValueError):
    """基板設定がドメイン制約を満たさない."""


class PasteFlowCalibrationBoardOverflowError(ValueError):
    """パターン群が指定された基板内に収まらない."""


@attrs.frozen
class PasteFlowCalibrationFootprintInfo:
    """検索可能なKiCad footprint."""

    footprint_id: str
    label: str
    library: str
    footprint: str


@attrs.frozen
class PasteFlowCalibrationPadPattern:
    """1 footprint 内で回転同値なパッドをまとめたカタログ項目."""

    catalog_id: str
    footprint_id: str
    footprint_label: str
    label: str
    family_id: str
    family_label: str
    library: str
    footprint: str
    source_pad_numbers: tuple[str, ...]
    source_pad_count: int
    pad_width_mm: float
    pad_height_mm: float
    default_rotation_span_deg: float
    default_rotation_count: int
    default_repeat_count: int


@attrs.frozen
class PasteFlowCalibrationBoardSpec:
    """基板外形と配置余白 [mm]."""

    width_mm: float = 40.0
    height_mm: float = 40.0
    edge_margin_mm: float = 1.0
    pad_gap_mm: float = 1.0


@attrs.frozen
class PasteFlowCalibrationPurgePadSpec:
    """左上に置く専用 purge pad の寸法 [mm]."""

    width_mm: float = 2.0
    height_mm: float = 2.0


@attrs.frozen
class PasteFlowCalibrationPattern:
    """1パッド種の回転列 × 繰り返し行."""

    catalog_id: str
    rotation_span_deg: float = 180.0
    rotation_count: int = 4
    repeat_count: int = 3


@attrs.frozen
class _DefaultFootprint:
    library: str
    footprint: str
    rotation_span_deg: float
    rotation_count: int
    repeat_count: int


_DEFAULT_FOOTPRINTS = (
    _DefaultFootprint("Resistor_SMD.pretty", "R_0402_1005Metric", 180.0, 4, 3),
    _DefaultFootprint("Resistor_SMD.pretty", "R_0603_1608Metric", 180.0, 4, 3),
    _DefaultFootprint("Resistor_SMD.pretty", "R_0805_2012Metric", 180.0, 4, 3),
    _DefaultFootprint("Resistor_SMD.pretty", "R_1206_3216Metric", 180.0, 4, 3),
    _DefaultFootprint("Package_TO_SOT_SMD.pretty", "SOT-23", 360.0, 4, 2),
    _DefaultFootprint("Package_TO_SOT_SMD.pretty", "SOT-23-5", 360.0, 4, 2),
)

# 空検索時に列挙する、はんだペースト印刷で一般的なSMD footprint。
# 個別footprintは検索で全ライブラリから選択できるため、ここでは代表寸法に絞る。
_COMMON_FOOTPRINTS = (
    ("Resistor_SMD.pretty", "R_0201_0603Metric"),
    ("Resistor_SMD.pretty", "R_0402_1005Metric"),
    ("Resistor_SMD.pretty", "R_0603_1608Metric"),
    ("Resistor_SMD.pretty", "R_0805_2012Metric"),
    ("Resistor_SMD.pretty", "R_1206_3216Metric"),
    ("Resistor_SMD.pretty", "R_1210_3225Metric"),
    ("Resistor_SMD.pretty", "R_2010_5025Metric"),
    ("Resistor_SMD.pretty", "R_2512_6332Metric"),
    ("Capacitor_SMD.pretty", "C_0201_0603Metric"),
    ("Capacitor_SMD.pretty", "C_0402_1005Metric"),
    ("Capacitor_SMD.pretty", "C_0603_1608Metric"),
    ("Capacitor_SMD.pretty", "C_0805_2012Metric"),
    ("Capacitor_SMD.pretty", "C_1206_3216Metric"),
    ("Capacitor_SMD.pretty", "C_1210_3225Metric"),
    ("Capacitor_SMD.pretty", "C_1812_4532Metric"),
    ("Inductor_SMD.pretty", "L_0201_0603Metric"),
    ("Inductor_SMD.pretty", "L_0402_1005Metric"),
    ("Inductor_SMD.pretty", "L_0603_1608Metric"),
    ("Inductor_SMD.pretty", "L_0805_2012Metric"),
    ("Inductor_SMD.pretty", "L_1206_3216Metric"),
    ("Inductor_SMD.pretty", "L_1210_3225Metric"),
    ("Fuse.pretty", "Fuse_0402_1005Metric"),
    ("Fuse.pretty", "Fuse_0603_1608Metric"),
    ("Fuse.pretty", "Fuse_0805_2012Metric"),
    ("Fuse.pretty", "Fuse_1206_3216Metric"),
    ("LED_SMD.pretty", "LED_0603_1608Metric"),
    ("LED_SMD.pretty", "LED_0805_2012Metric"),
    ("LED_SMD.pretty", "LED_1206_3216Metric"),
    ("Diode_SMD.pretty", "D_SOD-523"),
    ("Diode_SMD.pretty", "D_SOD-323"),
    ("Diode_SMD.pretty", "D_SOD-123"),
    ("Diode_SMD.pretty", "D_SMA"),
    ("Diode_SMD.pretty", "D_SMB"),
    ("Diode_SMD.pretty", "D_SMC"),
    ("Diode_SMD.pretty", "D_MicroMELF"),
    ("Diode_SMD.pretty", "D_MiniMELF"),
    ("Package_TO_SOT_SMD.pretty", "SOT-23"),
    ("Package_TO_SOT_SMD.pretty", "SOT-23-5"),
    ("Package_TO_SOT_SMD.pretty", "SOT-23-6"),
    ("Package_TO_SOT_SMD.pretty", "SOT-23-8"),
    ("Package_TO_SOT_SMD.pretty", "SOT-89-3"),
    ("Package_TO_SOT_SMD.pretty", "SOT-223-3_TabPin2"),
    ("Package_TO_SOT_SMD.pretty", "TO-252-3_TabPin2"),
    ("Package_TO_SOT_SMD.pretty", "TO-263-3_TabPin2"),
    ("Package_SO.pretty", "SOIC-8_3.9x4.9mm_P1.27mm"),
    ("Package_SO.pretty", "SOIC-14_3.9x8.7mm_P1.27mm"),
    ("Package_SO.pretty", "SOIC-16_3.9x9.9mm_P1.27mm"),
    ("Package_SO.pretty", "TSSOP-8_3x3mm_P0.65mm"),
    ("Package_SO.pretty", "TSSOP-14_4.4x5mm_P0.65mm"),
    ("Package_SO.pretty", "TSSOP-16_4.4x5mm_P0.65mm"),
    ("Package_SO.pretty", "TSSOP-20_4.4x6.5mm_P0.65mm"),
    ("Package_SO.pretty", "TSSOP-24_4.4x7.8mm_P0.65mm"),
    ("Package_SO.pretty", "TSSOP-28_4.4x9.7mm_P0.65mm"),
    ("Package_SO.pretty", "SSOP-16_4.4x5.2mm_P0.65mm"),
    ("Package_SO.pretty", "SSOP-20_4.4x6.5mm_P0.65mm"),
    ("Package_SO.pretty", "SSOP-28_5.3x10.2mm_P0.65mm"),
    ("Package_DFN_QFN.pretty", "DFN-8-1EP_2x2mm_P0.5mm_EP0.6x1.2mm"),
    ("Package_DFN_QFN.pretty", "QFN-16-1EP_3x3mm_P0.5mm_EP1.75x1.75mm"),
    ("Package_DFN_QFN.pretty", "QFN-24-1EP_4x4mm_P0.5mm_EP2.5x2.5mm"),
    ("Package_DFN_QFN.pretty", "QFN-32-1EP_5x5mm_P0.5mm_EP3.1x3.1mm"),
    ("Package_DFN_QFN.pretty", "QFN-48-1EP_7x7mm_P0.5mm_EP5.15x5.15mm"),
    ("Package_QFP.pretty", "LQFP-32_7x7mm_P0.8mm"),
    ("Package_QFP.pretty", "LQFP-48_7x7mm_P0.5mm"),
    ("Package_QFP.pretty", "LQFP-64_10x10mm_P0.5mm"),
    ("Package_QFP.pretty", "LQFP-100_14x14mm_P0.5mm"),
    ("Crystal.pretty", "Crystal_SMD_2012-2Pin_2.0x1.2mm"),
    ("Crystal.pretty", "Crystal_SMD_2520-4Pin_2.5x2.0mm"),
    ("Crystal.pretty", "Crystal_SMD_3225-4Pin_3.2x2.5mm"),
    ("Crystal.pretty", "Crystal_SMD_5032-4Pin_5.0x3.2mm"),
)
_DEFAULT_BY_SOURCE = {
    (item.library, item.footprint): item for item in _DEFAULT_FOOTPRINTS
}
_DEFAULT_SOURCE_ORDER = {
    (item.library, item.footprint): index
    for index, item in enumerate(_DEFAULT_FOOTPRINTS)
}


def _footprint_id(library: str, footprint: str) -> str:
    return f"{library}/{footprint}"


def _pad_catalog_id(library: str, footprint: str, pad_index: int) -> str:
    return f"{_footprint_id(library, footprint)}#pad-{pad_index}"


def _default_patterns() -> tuple[PasteFlowCalibrationPattern, ...]:
    return tuple(
        PasteFlowCalibrationPattern(
            catalog_id=_pad_catalog_id(item.library, item.footprint, 0),
            rotation_span_deg=item.rotation_span_deg,
            rotation_count=item.rotation_count,
            repeat_count=item.repeat_count,
        )
        for item in _DEFAULT_FOOTPRINTS
    )


@attrs.frozen
class PasteFlowCalibrationBoardConfig:
    """生成・preview・exportで共有する基板設定."""

    board: PasteFlowCalibrationBoardSpec = attrs.Factory(PasteFlowCalibrationBoardSpec)
    purge_pad: PasteFlowCalibrationPurgePadSpec = attrs.Factory(
        PasteFlowCalibrationPurgePadSpec
    )
    patterns: tuple[PasteFlowCalibrationPattern, ...] = attrs.Factory(_default_patterns)


@attrs.frozen
class PasteFlowCalibrationPoint:
    """基板左上原点の2D座標 [mm]."""

    x: float
    y: float


@attrs.frozen
class PasteFlowCalibrationPolygon:
    """preview用の解決済みパッドポリゴン."""

    layer: str
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
    """回転列 × 繰り返し行の解決済み矩形グループ."""

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
    pads: tuple[PasteFlowCalibrationPadLayout, ...]


@attrs.frozen
class PasteFlowCalibrationBoardLayout:
    """WebUIとKiCad生成が共有する完全に解決済みの配置."""

    board: PasteFlowCalibrationBoardSpec
    purge_pad: PasteFlowCalibrationBounds
    purge_polygons: tuple[PasteFlowCalibrationPolygon, ...]
    groups: tuple[PasteFlowCalibrationGroupLayout, ...]
    pad_count: int


@attrs.frozen
class _Envelope:
    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    @property
    def center_x(self) -> float:
        return (self.min_x + self.max_x) / 2.0

    @property
    def center_y(self) -> float:
        return (self.min_y + self.max_y) / 2.0


def validate_paste_flow_calibration_board_config(
    config: PasteFlowCalibrationBoardConfig,
) -> str | None:
    """構造的なドメイン制約を検証し、問題があれば説明を返す."""

    board = config.board
    positive = {
        "基板幅": board.width_mm,
        "基板高さ": board.height_mm,
        "purge pad幅": config.purge_pad.width_mm,
        "purge pad高さ": config.purge_pad.height_mm,
    }
    for label, value in positive.items():
        if not math.isfinite(value) or value <= 0:
            return f"{label}は正の有限値が必要です"
    nonnegative = {
        "外周余白": board.edge_margin_mm,
        "パッド間余白": board.pad_gap_mm,
    }
    for label, value in nonnegative.items():
        if not math.isfinite(value) or value < 0:
            return f"{label}は0以上の有限値が必要です"
    if board.width_mm <= 2 * board.edge_margin_mm:
        return "基板幅には左右の外周余白より大きい値が必要です"
    if board.height_mm <= 2 * board.edge_margin_mm:
        return "基板高さには上下の外周余白より大きい値が必要です"
    if not config.patterns:
        return "1つ以上のパッドパターンが必要です"

    seen: set[str] = set()
    for pattern in config.patterns:
        if _parse_pad_catalog_id(pattern.catalog_id) is None:
            return f"パッドパターンIDが不正です: {pattern.catalog_id}"
        if pattern.catalog_id in seen:
            return f"パッドパターンが重複しています: {pattern.catalog_id}"
        seen.add(pattern.catalog_id)
        if (
            not math.isfinite(pattern.rotation_span_deg)
            or pattern.rotation_span_deg <= 0
            or pattern.rotation_span_deg > 360
        ):
            return "回転範囲は0より大きく360以下で指定してください"
        if (
            isinstance(pattern.rotation_count, bool)
            or not isinstance(pattern.rotation_count, int)
            or pattern.rotation_count < 1
        ):
            return "回転分割数は1以上の整数が必要です"
        if (
            isinstance(pattern.repeat_count, bool)
            or not isinstance(pattern.repeat_count, int)
            or pattern.repeat_count < 1
        ):
            return "繰り返し行数は1以上の整数が必要です"
    return None


def normalize_paste_flow_calibration_board_config(
    config: PasteFlowCalibrationBoardConfig,
) -> PasteFlowCalibrationBoardConfig:
    """パターンをfamily・footprint・パッド種の安定順へ正規化する."""

    if (message := validate_paste_flow_calibration_board_config(config)) is not None:
        raise PasteFlowCalibrationBoardConfigError(message)
    return attrs.evolve(
        config,
        patterns=tuple(sorted(config.patterns, key=_pattern_sort_key)),
    )


def paste_flow_calibration_board_document(
    config: PasteFlowCalibrationBoardConfig,
) -> dict[str, Any]:
    """正規化済み設定を自己識別可能なJSON documentへ変換する."""

    normalized = normalize_paste_flow_calibration_board_config(config)
    return {
        "kind": PASTE_FLOW_CALIBRATION_BOARD_KIND,
        "schema_version": PASTE_FLOW_CALIBRATION_BOARD_SCHEMA_VERSION,
        "board": attrs.asdict(normalized.board),
        "purge_pad": attrs.asdict(normalized.purge_pad),
        "patterns": [attrs.asdict(pattern) for pattern in normalized.patterns],
    }


def parse_paste_flow_calibration_board_document(
    document: Mapping[str, object],
) -> PasteFlowCalibrationBoardConfig | None:
    """JSON documentを設定へ変換する。形式または値が不正ならNoneを返す."""

    if document.get("kind") != PASTE_FLOW_CALIBRATION_BOARD_KIND:
        return None
    if document.get("schema_version") != PASTE_FLOW_CALIBRATION_BOARD_SCHEMA_VERSION:
        return None
    board_data = document.get("board")
    purge_data = document.get("purge_pad")
    pattern_data = document.get("patterns")
    if not isinstance(board_data, Mapping):
        return None
    if not isinstance(purge_data, Mapping):
        return None
    if not isinstance(pattern_data, list):
        return None
    if set(board_data) != {
        "width_mm",
        "height_mm",
        "edge_margin_mm",
        "pad_gap_mm",
    }:
        return None
    if set(purge_data) != {"width_mm", "height_mm"}:
        return None
    patterns: list[PasteFlowCalibrationPattern] = []
    try:
        board = PasteFlowCalibrationBoardSpec(
            width_mm=_document_float(board_data["width_mm"]),
            height_mm=_document_float(board_data["height_mm"]),
            edge_margin_mm=_document_float(board_data["edge_margin_mm"]),
            pad_gap_mm=_document_float(board_data["pad_gap_mm"]),
        )
        purge = PasteFlowCalibrationPurgePadSpec(
            width_mm=_document_float(purge_data["width_mm"]),
            height_mm=_document_float(purge_data["height_mm"]),
        )
        for value in pattern_data:
            if not isinstance(value, Mapping):
                return None
            if set(value) != {
                "catalog_id",
                "rotation_span_deg",
                "rotation_count",
                "repeat_count",
            }:
                return None
            catalog_id = value["catalog_id"]
            if not isinstance(catalog_id, str):
                return None
            patterns.append(
                PasteFlowCalibrationPattern(
                    catalog_id=catalog_id,
                    rotation_span_deg=_document_float(value["rotation_span_deg"]),
                    rotation_count=_document_int(value["rotation_count"]),
                    repeat_count=_document_int(value["repeat_count"]),
                )
            )
    except (KeyError, TypeError, ValueError):
        return None
    config = PasteFlowCalibrationBoardConfig(
        board=board, purge_pad=purge, patterns=tuple(patterns)
    )
    if validate_paste_flow_calibration_board_config(config) is not None:
        return None
    return normalize_paste_flow_calibration_board_config(config)


def _document_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError
    return float(value)


def _document_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError
    return value


class PasteFlowCalibrationBoardGenerator:
    """KiCad footprintを検索し、単一パッド単位のlayoutと基板を生成する."""

    def __init__(self, footprint_root: Path | None = None) -> None:
        configured = os.environ.get("KICAD9_FOOTPRINT_DIR")
        self._footprint_root = (
            footprint_root
            if footprint_root is not None
            else Path(configured)
            if configured
            else DEFAULT_KICAD9_FOOTPRINT_DIR
        )
        self._footprint_index: tuple[PasteFlowCalibrationFootprintInfo, ...] | None = (
            None
        )
        self._pad_patterns: dict[str, tuple[PasteFlowCalibrationPadPattern, ...]] = {}
        self._pad_templates: dict[str, pcbnew.FOOTPRINT] = {}

    @property
    def catalog(self) -> tuple[PasteFlowCalibrationPadPattern, ...]:
        """初期レシピで使う解決済みパッドパターン."""

        return tuple(
            self._resolve_pad_pattern(pattern.catalog_id)[0]
            for pattern in _default_patterns()
        )

    @property
    def footprint_count(self) -> int:
        """検索対象となるKiCad footprint数."""

        return len(self._indexed_footprints())

    def search_footprints(
        self, query: str, limit: int = 30
    ) -> tuple[PasteFlowCalibrationFootprintInfo, ...]:
        """library名とfootprint名を空白区切りのAND検索する."""

        if limit < 1 or limit > 100:
            raise PasteFlowCalibrationBoardConfigError(
                "footprint検索件数は1以上100以下で指定してください"
            )
        footprints = self._indexed_footprints()
        tokens = _search_tokens(query)
        if not tokens:
            by_id = {item.footprint_id: item for item in footprints}
            return tuple(
                by_id[_footprint_id(library, footprint)]
                for library, footprint in _COMMON_FOOTPRINTS
                if _footprint_id(library, footprint) in by_id
            )[:limit]
        matches = [
            item
            for item in footprints
            if all(token in _search_text(item) for token in tokens)
        ]
        matches.sort(key=lambda item: _search_rank(item, query))
        return tuple(matches[:limit])

    def pad_patterns_for(
        self, footprint_id: str
    ) -> tuple[PasteFlowCalibrationPadPattern, ...]:
        """footprint内の回転同値なパッドを1種類ずつ返す."""

        cached = self._pad_patterns.get(footprint_id)
        if cached is not None:
            return cached
        parsed = _parse_footprint_id(footprint_id)
        if parsed is None:
            raise PasteFlowCalibrationBoardConfigError(
                f"footprint IDが不正です: {footprint_id}"
            )
        library, footprint_name = parsed
        footprint = self._load_footprint(library, footprint_name)
        grouped: dict[
            tuple[object, ...], tuple[int, pcbnew.FOOTPRINT, list[str], int]
        ] = {}
        for pad_index, pad in enumerate(footprint.Pads()):
            if not _has_relevant_layer(pad):
                continue
            template = _single_pad_footprint(pad)
            signature = _pad_geometry_signature(template)
            existing = grouped.get(signature)
            if existing is None:
                grouped[signature] = (pad_index, template, [pad.GetNumber()], 1)
            else:
                representative_index, representative, numbers, count = existing
                numbers.append(pad.GetNumber())
                grouped[signature] = (
                    representative_index,
                    representative,
                    numbers,
                    count + 1,
                )
        if not grouped:
            raise PasteFlowCalibrationBoardConfigError(
                f"F.Cu/F.Pasteパッドを持たないfootprintです: {footprint_id}"
            )

        family = library.removesuffix(".pretty")
        default = _DEFAULT_BY_SOURCE.get((library, footprint_name))
        patterns: list[PasteFlowCalibrationPadPattern] = []
        for pad_index, template, numbers, source_count in sorted(
            grouped.values(), key=lambda item: item[0]
        ):
            envelope = _footprint_envelope(template, 0.0)
            catalog_id = _pad_catalog_id(library, footprint_name, pad_index)
            source_numbers = _sorted_pad_numbers(numbers)
            item = PasteFlowCalibrationPadPattern(
                catalog_id=catalog_id,
                footprint_id=footprint_id,
                footprint_label=footprint_name,
                label=_pad_pattern_label(
                    source_numbers, source_count, envelope.width, envelope.height
                ),
                family_id=library,
                family_label=family,
                library=library,
                footprint=footprint_name,
                source_pad_numbers=source_numbers,
                source_pad_count=source_count,
                pad_width_mm=envelope.width,
                pad_height_mm=envelope.height,
                default_rotation_span_deg=(
                    default.rotation_span_deg if default is not None else 360.0
                ),
                default_rotation_count=(
                    default.rotation_count if default is not None else 4
                ),
                default_repeat_count=(
                    default.repeat_count if default is not None else 2
                ),
            )
            patterns.append(item)
            self._pad_templates[catalog_id] = template
        resolved = tuple(patterns)
        self._pad_patterns[footprint_id] = resolved
        return resolved

    def normalize_config(
        self, config: PasteFlowCalibrationBoardConfig
    ) -> PasteFlowCalibrationBoardConfig:
        """構造を正規化し、全パッドパターンが実footprintから解決可能か検証する."""

        normalized = normalize_paste_flow_calibration_board_config(config)
        for pattern in normalized.patterns:
            self._resolve_pad_pattern(pattern.catalog_id)
        return normalized

    def catalog_for_config(
        self, config: PasteFlowCalibrationBoardConfig
    ) -> tuple[PasteFlowCalibrationPadPattern, ...]:
        """設定に含まれるパッドパターンの表示情報を正規順で返す."""

        normalized = self.normalize_config(config)
        return tuple(
            self._resolve_pad_pattern(pattern.catalog_id)[0]
            for pattern in normalized.patterns
        )

    def layout(
        self, config: PasteFlowCalibrationBoardConfig
    ) -> PasteFlowCalibrationBoardLayout:
        """設定を検証し、単一パッド形状を持つ配置へ解決する."""

        normalized = self.normalize_config(config)
        resolved = {
            pattern.catalog_id: self._resolve_pad_pattern(pattern.catalog_id)
            for pattern in normalized.patterns
        }
        metrics = {
            pattern.catalog_id: self._group_metrics(
                pattern, resolved[pattern.catalog_id][1]
            )
            for pattern in normalized.patterns
        }
        placements = self._pack_groups(normalized, metrics, resolved)
        groups: list[PasteFlowCalibrationGroupLayout] = []
        pad_counter = 0
        for pattern in normalized.patterns:
            item, template = resolved[pattern.catalog_id]
            bounds = placements[pattern.catalog_id]
            cell_width, cell_height, angles, envelopes = metrics[pattern.catalog_id]
            pads: list[PasteFlowCalibrationPadLayout] = []
            for row in range(pattern.repeat_count):
                for column, angle in enumerate(angles):
                    envelope = envelopes[column]
                    cell_center_x = (
                        bounds.x
                        + column * (cell_width + normalized.board.pad_gap_mm)
                        + cell_width / 2.0
                    )
                    cell_center_y = (
                        bounds.y
                        + row * (cell_height + normalized.board.pad_gap_mm)
                        + cell_height / 2.0
                    )
                    anchor_x = cell_center_x - envelope.center_x
                    anchor_y = cell_center_y - envelope.center_y
                    pad_counter += 1
                    reference = f"PAD{pad_counter}"
                    placed = _duplicate_footprint(template)
                    placed.SetPosition(_vector(anchor_x, anchor_y))
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
                    pads=tuple(pads),
                )
            )
        purge = PasteFlowCalibrationBounds(
            x=normalized.board.edge_margin_mm,
            y=normalized.board.edge_margin_mm,
            width=normalized.purge_pad.width_mm,
            height=normalized.purge_pad.height_mm,
        )
        purge_points = _rectangle_points(purge)
        return PasteFlowCalibrationBoardLayout(
            board=normalized.board,
            purge_pad=purge,
            purge_polygons=(
                PasteFlowCalibrationPolygon("F.Cu", purge_points),
                PasteFlowCalibrationPolygon("F.Paste", purge_points),
            ),
            groups=tuple(groups),
            pad_count=sum(len(group.pads) for group in groups),
        )

    def build_board(self, config: PasteFlowCalibrationBoardConfig) -> pcbnew.BOARD:
        """解決済みlayoutと同じ位置へ単一パッドfootprintを置く."""

        layout = self.layout(config)
        board = generate_rect_pcb(layout.board.width_mm, layout.board.height_mm)
        self._add_purge_pad(board, layout.purge_pad)
        for group in layout.groups:
            item, template = self._resolve_pad_pattern(group.catalog_id)
            for pad_layout in group.pads:
                footprint = _duplicate_footprint(template)
                footprint.SetReference(pad_layout.reference)
                footprint.SetValue(f"{item.footprint_label} / {item.label}")
                footprint.SetPosition(_vector(pad_layout.x, pad_layout.y))
                footprint.SetOrientationDegrees(pad_layout.rotation_deg)
                footprint.Reference().SetVisible(False)
                footprint.Value().SetVisible(False)
                board.Add(footprint)
        return board

    def board_bytes(self, config: PasteFlowCalibrationBoardConfig) -> bytes:
        """生成したKiCad基板をダウンロード可能なbytesで返す."""

        board = self.build_board(config)
        with tempfile.TemporaryDirectory(prefix="pcbasm-paste-flow-board-") as temp:
            output = Path(temp) / "board.kicad_pcb"
            save_board(board, output)
            return output.read_bytes()

    def config_bytes(self, config: PasteFlowCalibrationBoardConfig) -> bytes:
        """設定JSONをUTF-8 bytesで返す."""

        normalized = self.normalize_config(config)
        document = paste_flow_calibration_board_document(normalized)
        return (
            json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        )

    def _indexed_footprints(self) -> tuple[PasteFlowCalibrationFootprintInfo, ...]:
        if self._footprint_index is not None:
            return self._footprint_index
        if not self._footprint_root.is_dir():
            raise PasteFlowCalibrationBoardEnvironmentError(
                f"KiCad footprint rootがありません: {self._footprint_root}"
            )
        footprints: list[PasteFlowCalibrationFootprintInfo] = []
        for library_path in sorted(self._footprint_root.glob("*.pretty")):
            if not library_path.is_dir():
                continue
            for path in sorted(library_path.glob("*.kicad_mod")):
                footprint = path.stem
                library = library_path.name
                family = library.removesuffix(".pretty")
                footprints.append(
                    PasteFlowCalibrationFootprintInfo(
                        footprint_id=_footprint_id(library, footprint),
                        label=f"{family} / {footprint}",
                        library=library,
                        footprint=footprint,
                    )
                )
        if not footprints:
            raise PasteFlowCalibrationBoardEnvironmentError(
                f"KiCad footprintがありません: {self._footprint_root}"
            )
        self._footprint_index = tuple(footprints)
        return self._footprint_index

    def _load_footprint(self, library: str, footprint: str) -> pcbnew.FOOTPRINT:
        library_path = self._footprint_root / library
        path = library_path / f"{footprint}.kicad_mod"
        if not path.is_file():
            raise PasteFlowCalibrationBoardEnvironmentError(
                f"KiCad footprintがありません: {path}"
            )
        loaded = pcbnew.FootprintLoad(str(library_path), footprint)
        if loaded is None:
            raise PasteFlowCalibrationBoardEnvironmentError(
                f"KiCad footprintを読み込めません: {path}"
            )
        return loaded

    def _resolve_pad_pattern(
        self, catalog_id: str
    ) -> tuple[PasteFlowCalibrationPadPattern, pcbnew.FOOTPRINT]:
        parsed = _parse_pad_catalog_id(catalog_id)
        if parsed is None:
            raise PasteFlowCalibrationBoardConfigError(
                f"パッドパターンIDが不正です: {catalog_id}"
            )
        library, footprint, _pad_index = parsed
        for item in self.pad_patterns_for(_footprint_id(library, footprint)):
            if item.catalog_id == catalog_id:
                return item, self._pad_templates[catalog_id]
        raise PasteFlowCalibrationBoardConfigError(
            f"footprintに指定のパッドパターンがありません: {catalog_id}"
        )

    @staticmethod
    def _group_metrics(
        pattern: PasteFlowCalibrationPattern,
        footprint: pcbnew.FOOTPRINT,
    ) -> tuple[float, float, tuple[float, ...], tuple[_Envelope, ...]]:
        angles = tuple(
            index * pattern.rotation_span_deg / pattern.rotation_count
            for index in range(pattern.rotation_count)
        )
        envelopes = tuple(_footprint_envelope(footprint, angle) for angle in angles)
        cell_width = max(envelope.width for envelope in envelopes)
        cell_height = max(envelope.height for envelope in envelopes)
        return cell_width, cell_height, angles, envelopes

    @staticmethod
    def _pack_groups(
        config: PasteFlowCalibrationBoardConfig,
        metrics: Mapping[
            str, tuple[float, float, tuple[float, ...], tuple[_Envelope, ...]]
        ],
        resolved: Mapping[str, tuple[PasteFlowCalibrationPadPattern, pcbnew.FOOTPRINT]],
    ) -> dict[str, PasteFlowCalibrationBounds]:
        board = config.board
        left = board.edge_margin_mm
        right = board.width_mm - board.edge_margin_mm
        bottom = board.height_mm - board.edge_margin_mm
        y = board.edge_margin_mm + config.purge_pad.height_mm + board.pad_gap_mm
        x = left
        row_height = 0.0
        family_id: str | None = None
        placements: dict[str, PasteFlowCalibrationBounds] = {}
        for pattern in config.patterns:
            item = resolved[pattern.catalog_id][0]
            cell_width, cell_height, _angles, _envelopes = metrics[pattern.catalog_id]
            width = (
                pattern.rotation_count * cell_width
                + (pattern.rotation_count - 1) * board.pad_gap_mm
            )
            height = (
                pattern.repeat_count * cell_height
                + (pattern.repeat_count - 1) * board.pad_gap_mm
            )
            if width > right - left + 1e-9:
                raise PasteFlowCalibrationBoardOverflowError(
                    f"{item.footprint_label} / {item.label}のグループ幅{width:.2f} mmが"
                    f"配置可能幅{right - left:.2f} mmを超えます"
                )
            if family_id is not None and item.family_id != family_id:
                y += row_height + board.pad_gap_mm
                x = left
                row_height = 0.0
            elif x > left and x + width > right + 1e-9:
                y += row_height + board.pad_gap_mm
                x = left
                row_height = 0.0
            if y + height > bottom + 1e-9:
                raise PasteFlowCalibrationBoardOverflowError(
                    f"{item.footprint_label} / {item.label}を配置すると基板高さを超えます"
                    f"（必要下端{y + height:.2f} mm、配置可能下端{bottom:.2f} mm）"
                )
            placements[pattern.catalog_id] = PasteFlowCalibrationBounds(
                x=x, y=y, width=width, height=height
            )
            x += width + board.pad_gap_mm
            row_height = max(row_height, height)
            family_id = item.family_id
        return placements

    @staticmethod
    def _add_purge_pad(board: pcbnew.BOARD, bounds: PasteFlowCalibrationBounds) -> None:
        center_x = bounds.x + bounds.width / 2.0
        center_y = bounds.y + bounds.height / 2.0
        footprint = pcbnew.FOOTPRINT(board)
        footprint.SetReference("PURGE1")
        footprint.SetValue("Paste purge")
        footprint.SetPosition(_vector(center_x, center_y))
        footprint.Reference().SetVisible(False)
        footprint.Value().SetVisible(False)
        pad = pcbnew.PAD(footprint)
        pad.SetNumber("1")
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetShape(pcbnew.PAD_SHAPE_RECTANGLE)
        pad.SetSize(_vector(bounds.width, bounds.height))
        pad.SetPosition(_vector(center_x, center_y))
        pad.SetLayerSet(pad.SMDMask())
        footprint.Add(pad)
        board.Add(footprint)


def _parse_footprint_id(footprint_id: str) -> tuple[str, str] | None:
    if footprint_id.count("/") != 1:
        return None
    library, footprint = footprint_id.split("/", 1)
    if not library.endswith(".pretty"):
        return None
    if not library or not footprint:
        return None
    if Path(library).name != library or Path(footprint).name != footprint:
        return None
    return library, footprint


def _parse_pad_catalog_id(catalog_id: str) -> tuple[str, str, int] | None:
    footprint_id, separator, index_text = catalog_id.rpartition("#pad-")
    parsed = _parse_footprint_id(footprint_id)
    if not separator or parsed is None or not index_text.isdigit():
        return None
    pad_index = int(index_text)
    if str(pad_index) != index_text:
        return None
    return parsed[0], parsed[1], pad_index


def _pattern_sort_key(pattern: PasteFlowCalibrationPattern) -> tuple[object, ...]:
    parsed = _parse_pad_catalog_id(pattern.catalog_id)
    if parsed is None:
        return (2, pattern.catalog_id.casefold())
    library, footprint, pad_index = parsed
    source = (library, footprint)
    if source in _DEFAULT_SOURCE_ORDER:
        return (0, _DEFAULT_SOURCE_ORDER[source], pad_index)
    return (1, library.casefold(), footprint.casefold(), pad_index)


def _search_tokens(query: str) -> tuple[str, ...]:
    return tuple(
        token for token in re.split(r"[\s_:/.-]+", query.casefold().strip()) if token
    )


def _search_text(item: PasteFlowCalibrationFootprintInfo) -> str:
    return " ".join(
        _search_tokens(f"{item.library.removesuffix('.pretty')} {item.footprint}")
    )


def _search_rank(
    item: PasteFlowCalibrationFootprintInfo, query: str
) -> tuple[object, ...]:
    normalized_query = " ".join(_search_tokens(query))
    footprint = " ".join(_search_tokens(item.footprint))
    if footprint == normalized_query:
        rank = 0
    elif footprint.startswith(normalized_query):
        rank = 1
    elif normalized_query in footprint:
        rank = 2
    else:
        rank = 3
    return rank, len(item.footprint), item.library.casefold(), item.footprint.casefold()


def _has_relevant_layer(pad: pcbnew.PAD) -> bool:
    layers = pad.GetLayerSet()
    return layers.Contains(pcbnew.F_Cu) or layers.Contains(pcbnew.F_Paste)


def _single_pad_footprint(pad: pcbnew.PAD) -> pcbnew.FOOTPRINT:
    duplicated = pad.Duplicate()
    if duplicated is None:
        raise PasteFlowCalibrationBoardEnvironmentError("KiCad padを複製できません")
    footprint = pcbnew.FOOTPRINT(None)
    duplicated.SetPosition(_vector(0.0, 0.0))
    duplicated.SetNumber("1")
    footprint.Add(duplicated)
    footprint.Reference().SetVisible(False)
    footprint.Value().SetVisible(False)
    return footprint


_SIGNATURE_LAYERS = (
    ("F.Cu", pcbnew.F_Cu),
    ("F.Mask", pcbnew.F_Mask),
    ("F.Paste", pcbnew.F_Paste),
)
_SIGNATURE_QUANTUM_NM = 1_000


def _pad_geometry_signature(footprint: pcbnew.FOOTPRINT) -> tuple[object, ...]:
    pad = next(iter(footprint.Pads()))
    drill = pad.GetDrillSize()
    drill_dimensions = tuple(
        sorted(
            (
                round(drill.x / _SIGNATURE_QUANTUM_NM),
                round(drill.y / _SIGNATURE_QUANTUM_NM),
            )
        )
    )
    geometries = tuple(
        _rotated_geometry_signature(footprint, angle)
        for angle in (0.0, 90.0, 180.0, 270.0)
    )
    return (
        int(pad.GetAttribute()),
        int(pad.GetDrillShape()),
        drill_dimensions,
        min(geometries),
    )


def _rotated_geometry_signature(
    footprint: pcbnew.FOOTPRINT, angle: float
) -> tuple[object, ...]:
    rotated = _duplicate_footprint(footprint)
    rotated.SetPosition(_vector(0.0, 0.0))
    rotated.SetOrientationDegrees(angle)
    pad = next(iter(rotated.Pads()))
    raw: list[tuple[str, tuple[tuple[int, int], ...]]] = []
    all_points: list[tuple[int, int]] = []
    for layer_name, layer in _SIGNATURE_LAYERS:
        if not pad.GetLayerSet().Contains(layer):
            continue
        shape = pad.GetEffectivePolygon(layer)
        for index in range(shape.OutlineCount()):
            points = tuple(
                (
                    round(point.x / _SIGNATURE_QUANTUM_NM),
                    round(point.y / _SIGNATURE_QUANTUM_NM),
                )
                for point in shape.Outline(index).CPoints()
            )
            if len(points) >= 3:
                raw.append((layer_name, points))
                all_points.extend(points)
    if not all_points:
        raise PasteFlowCalibrationBoardEnvironmentError(
            "padにF.Cu/F.Mask/F.Paste形状がありません"
        )
    min_x = min(point[0] for point in all_points)
    min_y = min(point[1] for point in all_points)
    normalized = [
        (
            layer_name,
            _canonical_contour(tuple((x - min_x, y - min_y) for x, y in points)),
        )
        for layer_name, points in raw
    ]
    return tuple(sorted(normalized))


def _canonical_contour(
    points: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    candidates: list[tuple[tuple[int, int], ...]] = []
    for sequence in (points, tuple(reversed(points))):
        candidates.extend(
            sequence[index:] + sequence[:index] for index in range(len(sequence))
        )
    return min(candidates)


def _sorted_pad_numbers(numbers: list[str]) -> tuple[str, ...]:
    unique = set(numbers)

    def key(value: str) -> tuple[int, int | str]:
        if value.isdigit():
            return 0, int(value)
        if value:
            return 1, value.casefold()
        return 2, ""

    return tuple(sorted(unique, key=key))


def _pad_pattern_label(
    numbers: tuple[str, ...], count: int, width: float, height: float
) -> str:
    numbered = [value for value in numbers if value]
    if not numbered:
        source = "Paste aperture"
    elif all(value.isdigit() for value in numbered):
        numeric = [int(value) for value in numbered]
        if len(numeric) > 1 and numeric == list(range(numeric[0], numeric[-1] + 1)):
            source = f"Pad {numeric[0]}–{numeric[-1]}"
        else:
            source = "Pad " + ", ".join(numbered)
    else:
        source = "Pad " + ", ".join(numbered)
    if count > 1:
        source += f" ×{count}"
    return f"{source} · {width:.3g} × {height:.3g} mm"


def _footprint_envelope(footprint: pcbnew.FOOTPRINT, angle: float) -> _Envelope:
    placed = _duplicate_footprint(footprint)
    placed.SetPosition(_vector(0.0, 0.0))
    placed.SetOrientationDegrees(angle)
    bounds: list[tuple[float, float, float, float]] = []
    for pad in placed.Pads():
        for _name, layer in _LAYERS:
            if not pad.GetLayerSet().Contains(layer):
                continue
            shape = pad.GetEffectivePolygon(layer)
            if shape.OutlineCount() < 1:
                continue
            box = shape.BBox()
            min_x = _to_mm(box.GetX())
            min_y = _to_mm(box.GetY())
            bounds.append(
                (
                    min_x,
                    min_y,
                    min_x + _to_mm(box.GetWidth()),
                    min_y + _to_mm(box.GetHeight()),
                )
            )
    if not bounds:
        raise PasteFlowCalibrationBoardEnvironmentError(
            "footprintにF.Cu/F.Pasteパッドがありません"
        )
    return _Envelope(
        min(item[0] for item in bounds),
        min(item[1] for item in bounds),
        max(item[2] for item in bounds),
        max(item[3] for item in bounds),
    )


def _duplicate_footprint(footprint: pcbnew.FOOTPRINT) -> pcbnew.FOOTPRINT:
    duplicated = footprint.Duplicate()
    if duplicated is None:
        raise PasteFlowCalibrationBoardEnvironmentError(
            "KiCad footprintを複製できません"
        )
    return duplicated


_LAYERS = (("F.Cu", pcbnew.F_Cu), ("F.Paste", pcbnew.F_Paste))


def _footprint_polygons(
    footprint: pcbnew.FOOTPRINT,
) -> tuple[PasteFlowCalibrationPolygon, ...]:
    polygons: list[PasteFlowCalibrationPolygon] = []
    for pad in footprint.Pads():
        for layer_name, layer in _LAYERS:
            if not pad.GetLayerSet().Contains(layer):
                continue
            shape = pad.GetEffectivePolygon(layer)
            for index in range(shape.OutlineCount()):
                points = tuple(
                    PasteFlowCalibrationPoint(_to_mm(point.x), _to_mm(point.y))
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


def _vector(x: float, y: float) -> pcbnew.VECTOR2I:
    return pcbnew.VECTOR2I(_from_mm(x), _from_mm(y))


def _from_mm(value: float) -> int:
    return cast(int, pcbnew.FromMM(value))


def _to_mm(value: int | float) -> float:
    return float(value) / 1_000_000.0


__all__ = [
    "PASTE_FLOW_CALIBRATION_BOARD_KIND",
    "PASTE_FLOW_CALIBRATION_BOARD_SCHEMA_VERSION",
    "PasteFlowCalibrationBoardConfig",
    "PasteFlowCalibrationBoardConfigError",
    "PasteFlowCalibrationBoardEnvironmentError",
    "PasteFlowCalibrationBoardGenerator",
    "PasteFlowCalibrationBoardLayout",
    "PasteFlowCalibrationBoardOverflowError",
    "PasteFlowCalibrationBoardSpec",
    "PasteFlowCalibrationBounds",
    "PasteFlowCalibrationFootprintInfo",
    "PasteFlowCalibrationGroupLayout",
    "PasteFlowCalibrationPadLayout",
    "PasteFlowCalibrationPadPattern",
    "PasteFlowCalibrationPattern",
    "PasteFlowCalibrationPoint",
    "PasteFlowCalibrationPolygon",
    "PasteFlowCalibrationPurgePadSpec",
    "normalize_paste_flow_calibration_board_config",
    "parse_paste_flow_calibration_board_document",
    "paste_flow_calibration_board_document",
    "validate_paste_flow_calibration_board_config",
]
