"""はんだペースト流量キャリブレーション基板の配置・生成.

KiCad footprint内の同一形状パッドを1つのパッドパターンとして抽出し、回転と
繰り返しからなる規則的なグループへ配置する。WebUIはこのモジュールが返す 解決済みlayoutを描画し、配置規則やパッド分類を持たない。
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import attrs
import pcbnew

from pcbasm.pcb.generate import generate_rect_pcb, save_board

PASTE_FLOW_CALIBRATION_BOARD_KIND = "paste_flow_calibration_board"
PASTE_FLOW_CALIBRATION_BOARD_SCHEMA_VERSION = 3
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
    default_transpose: bool


@attrs.frozen
class PasteFlowCalibrationCustomPadShape:
    """WebUIで選択できる任意寸法パッド形状."""

    shape: str
    label: str
    uses_height: bool
    uses_corner_radius: bool


PASTE_FLOW_CALIBRATION_CUSTOM_PAD_SHAPES = (
    PasteFlowCalibrationCustomPadShape("circle", "円", False, False),
    PasteFlowCalibrationCustomPadShape("rectangle", "矩形", True, False),
    PasteFlowCalibrationCustomPadShape("roundrect", "角丸矩形", True, True),
    PasteFlowCalibrationCustomPadShape("oval", "長円（スロット）", True, False),
)
_CUSTOM_PAD_SHAPE_BY_ID = {
    item.shape: item for item in PASTE_FLOW_CALIBRATION_CUSTOM_PAD_SHAPES
}


@attrs.frozen
class PasteFlowCalibrationCustomPadDraft:
    """ID採番前の任意寸法パッド定義 [mm]."""

    shape: str
    width_mm: float
    height_mm: float
    corner_radius_mm: float = 0.0
    name: str = ""


@attrs.frozen
class PasteFlowCalibrationCustomPadSpec:
    """設定JSONへ保存する任意寸法パッド定義 [mm]."""

    catalog_id: str
    name: str
    shape: str
    width_mm: float
    height_mm: float
    corner_radius_mm: float = 0.0


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
    """1パッド種の回転・繰り返し配置."""

    catalog_id: str
    rotation_span_deg: float = 180.0
    rotation_count: int = 4
    repeat_count: int = 3
    transpose: bool = False


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
    _DefaultFootprint("Package_TO_SOT_SMD.pretty", "SOT-23", 180.0, 4, 2),
    _DefaultFootprint("Package_TO_SOT_SMD.pretty", "SOT-23-5", 180.0, 4, 2),
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

    auto_pack: bool = True
    board: PasteFlowCalibrationBoardSpec = attrs.Factory(PasteFlowCalibrationBoardSpec)
    purge_pad: PasteFlowCalibrationPurgePadSpec = attrs.Factory(
        PasteFlowCalibrationPurgePadSpec
    )
    custom_pads: tuple[PasteFlowCalibrationCustomPadSpec, ...] = ()
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


def validate_paste_flow_calibration_board_config(
    config: PasteFlowCalibrationBoardConfig,
) -> str | None:
    """構造的なドメイン制約を検証し、問題があれば説明を返す."""

    if not isinstance(config.auto_pack, bool):
        return "自動最適配置は真偽値で指定してください"
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

    custom_ids: set[str] = set()
    for custom_pad in config.custom_pads:
        if not _is_custom_pad_catalog_id(custom_pad.catalog_id):
            return f"任意パッドIDが不正です: {custom_pad.catalog_id}"
        if custom_pad.catalog_id in custom_ids:
            return f"任意パッドが重複しています: {custom_pad.catalog_id}"
        custom_ids.add(custom_pad.catalog_id)
        if (message := _validate_custom_pad(custom_pad)) is not None:
            return message

    seen: set[str] = set()
    for pattern in config.patterns:
        if _parse_pad_catalog_id(
            pattern.catalog_id
        ) is None and not _is_custom_pad_catalog_id(pattern.catalog_id):
            return f"パッドパターンIDが不正です: {pattern.catalog_id}"
        if (
            _is_custom_pad_catalog_id(pattern.catalog_id)
            and pattern.catalog_id not in custom_ids
        ):
            return f"任意パッド定義がありません: {pattern.catalog_id}"
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
            return "繰り返し数は1以上の整数が必要です"
        if not isinstance(pattern.transpose, bool):
            return "転置配置は真偽値で指定してください"
    return None


def normalize_paste_flow_calibration_board_config(
    config: PasteFlowCalibrationBoardConfig,
) -> PasteFlowCalibrationBoardConfig:
    """パターンをfamily・footprint・パッド種の安定順へ正規化する."""

    if (message := validate_paste_flow_calibration_board_config(config)) is not None:
        raise PasteFlowCalibrationBoardConfigError(message)
    custom_by_id = {item.catalog_id: item for item in config.custom_pads}
    patterns = tuple(
        sorted(
            config.patterns,
            key=lambda pattern: _pattern_sort_key(pattern, custom_by_id),
        )
    )
    used_custom_ids = {
        pattern.catalog_id
        for pattern in patterns
        if _is_custom_pad_catalog_id(pattern.catalog_id)
    }
    return attrs.evolve(
        config,
        custom_pads=tuple(
            sorted(
                (
                    item
                    for item in config.custom_pads
                    if item.catalog_id in used_custom_ids
                ),
                key=lambda item: (item.name.casefold(), item.catalog_id),
            )
        ),
        patterns=patterns,
    )


def paste_flow_calibration_board_document(
    config: PasteFlowCalibrationBoardConfig,
) -> dict[str, Any]:
    """正規化済み設定を自己識別可能なJSON documentへ変換する."""

    normalized = normalize_paste_flow_calibration_board_config(config)
    return {
        "kind": PASTE_FLOW_CALIBRATION_BOARD_KIND,
        "schema_version": PASTE_FLOW_CALIBRATION_BOARD_SCHEMA_VERSION,
        "auto_pack": normalized.auto_pack,
        "board": attrs.asdict(normalized.board),
        "purge_pad": attrs.asdict(normalized.purge_pad),
        "custom_pads": [attrs.asdict(item) for item in normalized.custom_pads],
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
    if set(document) != {
        "kind",
        "schema_version",
        "auto_pack",
        "board",
        "purge_pad",
        "custom_pads",
        "patterns",
    }:
        return None
    auto_pack = document.get("auto_pack")
    board_data = document.get("board")
    purge_data = document.get("purge_pad")
    custom_pad_data = document.get("custom_pads")
    pattern_data = document.get("patterns")
    if not isinstance(auto_pack, bool):
        return None
    if not isinstance(board_data, Mapping):
        return None
    if not isinstance(purge_data, Mapping):
        return None
    if not isinstance(custom_pad_data, list):
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
    custom_pads: list[PasteFlowCalibrationCustomPadSpec] = []
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
        for value in custom_pad_data:
            if not isinstance(value, Mapping):
                return None
            if set(value) != {
                "catalog_id",
                "name",
                "shape",
                "width_mm",
                "height_mm",
                "corner_radius_mm",
            }:
                return None
            catalog_id = value["catalog_id"]
            name = value["name"]
            shape = value["shape"]
            if not all(isinstance(item, str) for item in (catalog_id, name, shape)):
                return None
            custom_pads.append(
                PasteFlowCalibrationCustomPadSpec(
                    catalog_id=catalog_id,
                    name=name,
                    shape=shape,
                    width_mm=_document_float(value["width_mm"]),
                    height_mm=_document_float(value["height_mm"]),
                    corner_radius_mm=_document_float(value["corner_radius_mm"]),
                )
            )
        for value in pattern_data:
            if not isinstance(value, Mapping):
                return None
            expected_fields = {
                "catalog_id",
                "rotation_span_deg",
                "rotation_count",
                "repeat_count",
                "transpose",
            }
            if set(value) != expected_fields:
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
                    transpose=_document_bool(value["transpose"]),
                )
            )
    except (KeyError, TypeError, ValueError):
        return None
    config = PasteFlowCalibrationBoardConfig(
        auto_pack=auto_pack,
        board=board,
        purge_pad=purge,
        custom_pads=tuple(custom_pads),
        patterns=tuple(patterns),
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


def _document_bool(value: object) -> bool:
    if not isinstance(value, bool):
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
                    default.rotation_span_deg if default is not None else 180.0
                ),
                default_rotation_count=(
                    default.rotation_count if default is not None else 4
                ),
                default_repeat_count=(
                    default.repeat_count if default is not None else 2
                ),
                default_transpose=False,
            )
            patterns.append(item)
            self._pad_templates[catalog_id] = template
        resolved = tuple(patterns)
        self._pad_patterns[footprint_id] = resolved
        return resolved

    def add_custom_pad(
        self,
        config: PasteFlowCalibrationBoardConfig,
        draft: PasteFlowCalibrationCustomPadDraft,
    ) -> PasteFlowCalibrationBoardConfig:
        """任意寸法パッドを設定へ追加し、採番済み設定を返す."""

        if (message := _validate_custom_pad(draft)) is not None:
            raise PasteFlowCalibrationBoardConfigError(message)
        catalog_id = f"custom:{uuid.uuid4().hex}"
        custom_pad = PasteFlowCalibrationCustomPadSpec(
            catalog_id=catalog_id,
            name=draft.name.strip() or _default_custom_pad_name(draft),
            shape=draft.shape,
            width_mm=draft.width_mm,
            height_mm=draft.height_mm,
            corner_radius_mm=draft.corner_radius_mm,
        )
        return self.normalize_config(
            attrs.evolve(
                config,
                custom_pads=(*config.custom_pads, custom_pad),
                patterns=(
                    *config.patterns,
                    PasteFlowCalibrationPattern(catalog_id=catalog_id),
                ),
            )
        )

    def normalize_config(
        self, config: PasteFlowCalibrationBoardConfig
    ) -> PasteFlowCalibrationBoardConfig:
        """構造を正規化し、全パッドパターンが実footprintから解決可能か検証する."""

        normalized = normalize_paste_flow_calibration_board_config(config)
        custom_by_id = {item.catalog_id: item for item in normalized.custom_pads}
        for pattern in normalized.patterns:
            self._resolve_pattern(pattern.catalog_id, custom_by_id)
        return normalized

    def catalog_for_config(
        self, config: PasteFlowCalibrationBoardConfig
    ) -> tuple[PasteFlowCalibrationPadPattern, ...]:
        """設定に含まれるパッドパターンの表示情報を正規順で返す."""

        normalized = self.normalize_config(config)
        custom_by_id = {item.catalog_id: item for item in normalized.custom_pads}
        return tuple(
            self._resolve_pattern(pattern.catalog_id, custom_by_id)[0]
            for pattern in normalized.patterns
        )

    def layout(
        self, config: PasteFlowCalibrationBoardConfig
    ) -> PasteFlowCalibrationBoardLayout:
        """設定を検証し、単一パッド形状を持つ配置へ解決する."""

        normalized = self.normalize_config(config)
        custom_by_id = {item.catalog_id: item for item in normalized.custom_pads}
        resolved = {
            pattern.catalog_id: self._resolve_pattern(pattern.catalog_id, custom_by_id)
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
                    transpose=transpose,
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

        normalized = self.normalize_config(config)
        custom_by_id = {item.catalog_id: item for item in normalized.custom_pads}
        resolved = {
            pattern.catalog_id: self._resolve_pattern(pattern.catalog_id, custom_by_id)
            for pattern in normalized.patterns
        }
        layout = self.layout(normalized)
        board = generate_rect_pcb(layout.board.width_mm, layout.board.height_mm)
        self._add_purge_pad(board, layout.purge_pad)
        for group in layout.groups:
            item, template = resolved[group.catalog_id]
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

    def _resolve_pattern(
        self,
        catalog_id: str,
        custom_by_id: Mapping[str, PasteFlowCalibrationCustomPadSpec],
    ) -> tuple[PasteFlowCalibrationPadPattern, pcbnew.FOOTPRINT]:
        custom_pad = custom_by_id.get(catalog_id)
        if custom_pad is None:
            return self._resolve_pad_pattern(catalog_id)
        template = _custom_pad_footprint(custom_pad)
        envelope = _footprint_envelope(template, 0.0)
        shape = _CUSTOM_PAD_SHAPE_BY_ID[custom_pad.shape]
        radius = (
            f" · R{custom_pad.corner_radius_mm:.3g} mm"
            if custom_pad.shape == "roundrect"
            else ""
        )
        item = PasteFlowCalibrationPadPattern(
            catalog_id=catalog_id,
            footprint_id=catalog_id,
            footprint_label=custom_pad.name,
            label=(
                f"{shape.label} · {envelope.width:.3g} × {envelope.height:.3g} mm"
                f"{radius}"
            ),
            family_id="custom",
            family_label="任意サイズ",
            library="",
            footprint=custom_pad.name,
            source_pad_numbers=("1",),
            source_pad_count=1,
            pad_width_mm=envelope.width,
            pad_height_mm=envelope.height,
            default_rotation_span_deg=180.0,
            default_rotation_count=4,
            default_repeat_count=3,
            default_transpose=False,
        )
        return item, template

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
    ) -> dict[str, _PackedGroup]:
        _validate_purge_region(config)
        if config.auto_pack:
            return _pack_groups_optimized(config, metrics, resolved)
        return _pack_groups_ordered(config, metrics, resolved)

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
    metrics: tuple[float, float, tuple[float, ...], tuple[_Envelope, ...]],
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
    metrics: Mapping[
        str, tuple[float, float, tuple[float, ...], tuple[_Envelope, ...]]
    ],
    resolved: Mapping[str, tuple[PasteFlowCalibrationPadPattern, pcbnew.FOOTPRINT]],
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
        item = resolved[pattern.catalog_id][0]
        width, height = _group_dimensions(
            pattern,
            metrics[pattern.catalog_id],
            board.pad_gap_mm,
            pattern.transpose,
        )
        if width > area.width + 1e-9:
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
        if y + height > bottom + 1e-9:
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
    metrics: Mapping[
        str, tuple[float, float, tuple[float, ...], tuple[_Envelope, ...]]
    ],
    resolved: Mapping[str, tuple[PasteFlowCalibrationPadPattern, pcbnew.FOOTPRINT]],
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
            item = resolved[pattern.catalog_id][0]
            raise PasteFlowCalibrationBoardOverflowError(
                f"{item.footprint_label} / {item.label}は転置しても"
                f"配置領域{area.width:.2f} × {area.height:.2f} mmに収まりません"
            )

    attempts: list[dict[str, _PackedGroup]] = []
    for order in _packing_orders(config.patterns, variants):
        for heuristic in ("short_side", "area", "bottom_left"):
            packed = _pack_max_rects(
                order,
                variants,
                area,
                purge_keepout,
                config.board.pad_gap_mm,
                heuristic,
            )
            if packed is not None:
                attempts.append(packed)
    if not attempts:
        raise PasteFlowCalibrationBoardOverflowError(
            "自動最適配置でもすべてのパッドグループを配置できず、"
            "基板の配置可能領域を超えます"
        )
    return min(attempts, key=lambda packed: _packing_score(packed, area))


def _packing_variants(
    pattern: PasteFlowCalibrationPattern,
    metrics: Mapping[
        str, tuple[float, float, tuple[float, ...], tuple[_Envelope, ...]]
    ],
    gap: float,
) -> tuple[tuple[bool, float, float], ...]:
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
    variants: Mapping[str, tuple[tuple[bool, float, float], ...]],
) -> tuple[tuple[PasteFlowCalibrationPattern, ...], ...]:
    def maximum(catalog_id: str, value: int) -> float:
        return max(item[value] for item in variants[catalog_id])

    keys = (
        lambda pattern: (0,),
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
    orders: list[tuple[PasteFlowCalibrationPattern, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for index, key in enumerate(keys):
        order = patterns if index == 0 else tuple(sorted(patterns, key=key))
        identity = tuple(pattern.catalog_id for pattern in order)
        if identity not in seen:
            orders.append(order)
            seen.add(identity)
    return tuple(orders)


def _pack_max_rects(
    patterns: tuple[PasteFlowCalibrationPattern, ...],
    variants: Mapping[str, tuple[tuple[bool, float, float], ...]],
    area: _PackingRect,
    purge_keepout: _PackingRect,
    gap: float,
    heuristic: str,
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
    heuristic: str,
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


def _prune_free_rectangles(rectangles: list[_PackingRect]) -> tuple[_PackingRect, ...]:
    useful = [
        rectangle
        for rectangle in rectangles
        if rectangle.width > 1e-9 and rectangle.height > 1e-9
    ]
    return tuple(
        rectangle
        for index, rectangle in enumerate(useful)
        if not any(
            index != other_index and _contains(other, rectangle)
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


_CUSTOM_PAD_ID_PATTERN = re.compile(r"custom:[0-9a-f]{32}")


def _is_custom_pad_catalog_id(catalog_id: str) -> bool:
    return _CUSTOM_PAD_ID_PATTERN.fullmatch(catalog_id) is not None


def _validate_custom_pad(
    custom_pad: PasteFlowCalibrationCustomPadDraft | PasteFlowCalibrationCustomPadSpec,
) -> str | None:
    name = custom_pad.name.strip()
    if isinstance(custom_pad, PasteFlowCalibrationCustomPadSpec) and not name:
        return "任意パッドの名称を入力してください"
    if len(name) > 120:
        return "任意パッドの名称は120文字以下で指定してください"
    shape = _CUSTOM_PAD_SHAPE_BY_ID.get(custom_pad.shape)
    if shape is None:
        return f"任意パッド形状が不正です: {custom_pad.shape}"
    dimensions = {
        "幅／直径": custom_pad.width_mm,
        "高さ": custom_pad.height_mm,
    }
    for label, value in dimensions.items():
        if not math.isfinite(value) or value <= 0:
            return f"任意パッドの{label}は正の有限値が必要です"
    if not math.isfinite(custom_pad.corner_radius_mm):
        return "任意パッドの角丸半径は有限値が必要です"
    if custom_pad.shape == "circle" and not math.isclose(
        custom_pad.width_mm, custom_pad.height_mm, abs_tol=1e-9
    ):
        return "円パッドの幅と高さには同じ直径を指定してください"
    if shape.uses_corner_radius:
        if custom_pad.corner_radius_mm <= 0:
            return "角丸矩形の角丸半径は0より大きい値が必要です"
        if (
            custom_pad.corner_radius_mm
            > min(custom_pad.width_mm, custom_pad.height_mm) / 2.0
        ):
            return "角丸矩形の角丸半径は短辺の半分以下で指定してください"
    elif custom_pad.corner_radius_mm != 0:
        return f"{shape.label}では角丸半径を指定できません"
    return None


def _default_custom_pad_name(custom_pad: PasteFlowCalibrationCustomPadDraft) -> str:
    shape = _CUSTOM_PAD_SHAPE_BY_ID[custom_pad.shape]
    if custom_pad.shape == "circle":
        dimensions = f"φ{custom_pad.width_mm:.3g} mm"
    else:
        dimensions = f"{custom_pad.width_mm:.3g} × {custom_pad.height_mm:.3g} mm"
    radius = (
        f" R{custom_pad.corner_radius_mm:.3g} mm"
        if custom_pad.shape == "roundrect"
        else ""
    )
    return f"{shape.label} {dimensions}{radius}"


def _pattern_sort_key(
    pattern: PasteFlowCalibrationPattern,
    custom_by_id: Mapping[str, PasteFlowCalibrationCustomPadSpec],
) -> tuple[object, ...]:
    parsed = _parse_pad_catalog_id(pattern.catalog_id)
    if parsed is None:
        custom_pad = custom_by_id.get(pattern.catalog_id)
        if custom_pad is None:
            return (3, pattern.catalog_id.casefold())
        return (
            2,
            custom_pad.name.casefold(),
            custom_pad.shape,
            custom_pad.width_mm,
            custom_pad.height_mm,
            pattern.catalog_id,
        )
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


def _custom_pad_footprint(
    custom_pad: PasteFlowCalibrationCustomPadSpec,
) -> pcbnew.FOOTPRINT:
    footprint = pcbnew.FOOTPRINT(None)
    pad = pcbnew.PAD(footprint)
    pad.SetNumber("1")
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    shape = {
        "circle": pcbnew.PAD_SHAPE_CIRCLE,
        "rectangle": pcbnew.PAD_SHAPE_RECTANGLE,
        "roundrect": pcbnew.PAD_SHAPE_ROUNDRECT,
        "oval": pcbnew.PAD_SHAPE_OVAL,
    }[custom_pad.shape]
    pad.SetShape(shape)
    pad.SetSize(_vector(custom_pad.width_mm, custom_pad.height_mm))
    pad.SetPosition(_vector(0.0, 0.0))
    pad.SetLayerSet(pad.SMDMask())
    if custom_pad.shape == "roundrect":
        pad.SetRoundRectRadiusRatio(
            custom_pad.corner_radius_mm / min(custom_pad.width_mm, custom_pad.height_mm)
        )
    footprint.Add(pad)
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
    "PASTE_FLOW_CALIBRATION_CUSTOM_PAD_SHAPES",
    "PasteFlowCalibrationBoardConfig",
    "PasteFlowCalibrationBoardConfigError",
    "PasteFlowCalibrationBoardEnvironmentError",
    "PasteFlowCalibrationBoardGenerator",
    "PasteFlowCalibrationBoardLayout",
    "PasteFlowCalibrationBoardOverflowError",
    "PasteFlowCalibrationBoardSpec",
    "PasteFlowCalibrationBounds",
    "PasteFlowCalibrationCustomPadDraft",
    "PasteFlowCalibrationCustomPadShape",
    "PasteFlowCalibrationCustomPadSpec",
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
