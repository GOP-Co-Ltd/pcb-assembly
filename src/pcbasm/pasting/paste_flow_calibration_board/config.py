"""はんだペースト流量キャリブレーション基板の設定とJSON形式."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any, Literal, TypeAlias, TypeGuard

import attrs

from pcbasm.pcb.footprint import format_footprint_id, parse_footprint_id
from pcbasm.pcb.units import (
    KICAD_COORD_MAX_NM,
    KICAD_MAX_COORD_MM,
    KicadError,
    is_kicad_length,
)
from pcbasm.utils import is_finite_number

PasteFlowCalibrationBoardKind: TypeAlias = Literal["paste_flow_calibration_board"]
PasteFlowCalibrationBoardSchemaVersion: TypeAlias = Literal[1]
PasteFlowCalibrationCustomPadShapeId: TypeAlias = Literal[
    "circle", "rectangle", "roundrect", "oval"
]
PasteFlowCalibrationPreviewLayer: TypeAlias = Literal["F.Cu", "F.Paste"]

PASTE_FLOW_CALIBRATION_BOARD_KIND: PasteFlowCalibrationBoardKind = (
    "paste_flow_calibration_board"
)
PASTE_FLOW_CALIBRATION_BOARD_SCHEMA_VERSION: PasteFlowCalibrationBoardSchemaVersion = 1
_MAX_CALIBRATION_PAD_COUNT = 10_000
_KICAD_MAX_PAD_SIZE_MM = (KICAD_COORD_MAX_NM - 1) / 1_000_000
_KICAD_LENGTH_RANGE_TEXT = f"1 nm以上{KICAD_MAX_COORD_MM:.6f} mm以下"
_KICAD_PAD_SIZE_RANGE_TEXT = f"1 nm以上{_KICAD_MAX_PAD_SIZE_MM:.6f} mm以下"


# KiCad 環境・座標エラーは pcbasm.pcb 側の例外をそのまま使う
PasteFlowCalibrationBoardEnvironmentError = KicadError


class PasteFlowCalibrationBoardConfigError(ValueError):
    """基板設定がドメイン制約を満たさない."""


class PasteFlowCalibrationBoardOverflowError(ValueError):
    """パターン群が指定された基板内に収まらない."""


@attrs.frozen
class PasteFlowCalibrationCustomPadShape:
    """WebUIで選択できる任意寸法パッド形状."""

    shape: PasteFlowCalibrationCustomPadShapeId
    label: str
    uses_height: bool
    uses_corner_radius: bool


PASTE_FLOW_CALIBRATION_CUSTOM_PAD_SHAPES: tuple[
    PasteFlowCalibrationCustomPadShape, ...
] = (
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

    shape: PasteFlowCalibrationCustomPadShapeId
    width_mm: float
    height_mm: float = 0.0
    corner_radius_mm: float = 0.0
    name: str = ""


@attrs.frozen
class PasteFlowCalibrationCustomPadSpec:
    """設定JSONへ保存する任意寸法パッド定義 [mm]."""

    catalog_id: str
    name: str
    shape: PasteFlowCalibrationCustomPadShapeId
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
    """左上に置く専用purge padの寸法 [mm]."""

    width_mm: float = 2.0
    height_mm: float = 2.0


@attrs.frozen
class PasteFlowCalibrationPattern:
    """1パッド種の回転・繰り返し配置."""

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


DEFAULT_FOOTPRINTS = (
    _DefaultFootprint("Resistor_SMD.pretty", "R_0402_1005Metric", 180.0, 4, 3),
    _DefaultFootprint("Resistor_SMD.pretty", "R_0603_1608Metric", 180.0, 4, 3),
    _DefaultFootprint("Resistor_SMD.pretty", "R_0805_2012Metric", 180.0, 4, 3),
    _DefaultFootprint("Resistor_SMD.pretty", "R_1206_3216Metric", 180.0, 4, 3),
    _DefaultFootprint("Package_TO_SOT_SMD.pretty", "SOT-23", 180.0, 4, 2),
    _DefaultFootprint("Package_TO_SOT_SMD.pretty", "SOT-23-5", 180.0, 4, 2),
)
DEFAULT_FOOTPRINT_BY_SOURCE = {
    (item.library, item.footprint): item for item in DEFAULT_FOOTPRINTS
}
_DEFAULT_SOURCE_ORDER = {
    (item.library, item.footprint): index
    for index, item in enumerate(DEFAULT_FOOTPRINTS)
}


def format_pad_catalog_id(library: str, footprint: str, pad_index: int) -> str:
    return f"{format_footprint_id(library, footprint)}#pad-{pad_index}"


def default_patterns() -> tuple[PasteFlowCalibrationPattern, ...]:
    return tuple(
        PasteFlowCalibrationPattern(
            catalog_id=format_pad_catalog_id(item.library, item.footprint, 0),
            rotation_span_deg=item.rotation_span_deg,
            rotation_count=item.rotation_count,
            repeat_count=item.repeat_count,
        )
        for item in DEFAULT_FOOTPRINTS
    )


@attrs.frozen
class PasteFlowCalibrationBoardConfig:
    """生成・preview・exportで共有する基板設定."""

    board: PasteFlowCalibrationBoardSpec = attrs.Factory(PasteFlowCalibrationBoardSpec)
    purge_pad: PasteFlowCalibrationPurgePadSpec = attrs.Factory(
        PasteFlowCalibrationPurgePadSpec
    )
    custom_pads: tuple[PasteFlowCalibrationCustomPadSpec, ...] = ()
    patterns: tuple[PasteFlowCalibrationPattern, ...] = attrs.Factory(default_patterns)


def validate_paste_flow_calibration_board_config(
    config: PasteFlowCalibrationBoardConfig,
) -> str | None:
    """構造的なドメイン制約を検証し、問題があれば説明を返す."""

    if not isinstance(config, PasteFlowCalibrationBoardConfig):
        return "基板設定の形式が不正です"
    if not isinstance(config.board, PasteFlowCalibrationBoardSpec):
        return "基板外形の形式が不正です"
    if not isinstance(config.purge_pad, PasteFlowCalibrationPurgePadSpec):
        return "purge padの形式が不正です"
    if not isinstance(config.custom_pads, tuple):
        return "任意パッド一覧の形式が不正です"
    if not isinstance(config.patterns, tuple):
        return "パッドパターン一覧の形式が不正です"

    board = config.board
    positive = (
        ("基板幅", board.width_mm, KICAD_MAX_COORD_MM, _KICAD_LENGTH_RANGE_TEXT),
        (
            "基板高さ",
            board.height_mm,
            KICAD_MAX_COORD_MM,
            _KICAD_LENGTH_RANGE_TEXT,
        ),
        (
            "purge pad幅",
            config.purge_pad.width_mm,
            _KICAD_MAX_PAD_SIZE_MM,
            _KICAD_PAD_SIZE_RANGE_TEXT,
        ),
        (
            "purge pad高さ",
            config.purge_pad.height_mm,
            _KICAD_MAX_PAD_SIZE_MM,
            _KICAD_PAD_SIZE_RANGE_TEXT,
        ),
    )
    for label, value, maximum_mm, range_text in positive:
        if not is_finite_number(value) or value <= 0:
            return f"{label}は正の有限値が必要です"
        if not is_kicad_length(value, maximum_mm=maximum_mm):
            return f"{label}は{range_text}で指定してください"
    nonnegative = (
        ("外周余白", board.edge_margin_mm),
        ("パッド間余白", board.pad_gap_mm),
    )
    for label, value in nonnegative:
        if not is_finite_number(value) or value < 0:
            return f"{label}は0以上の有限値が必要です"
        if value != 0 and not is_kicad_length(value):
            return f"{label}は0または{_KICAD_LENGTH_RANGE_TEXT}で指定してください"
    if board.width_mm <= 2 * board.edge_margin_mm:
        return "基板幅には左右の外周余白より大きい値が必要です"
    if board.height_mm <= 2 * board.edge_margin_mm:
        return "基板高さには上下の外周余白より大きい値が必要です"
    if not config.patterns:
        return "1つ以上のパッドパターンが必要です"

    custom_ids: set[str] = set()
    for custom_pad in config.custom_pads:
        if not isinstance(custom_pad, PasteFlowCalibrationCustomPadSpec):
            return "任意パッドの形式が不正です"
        if not is_custom_pad_catalog_id(custom_pad.catalog_id):
            return f"任意パッドIDが不正です: {custom_pad.catalog_id}"
        if custom_pad.catalog_id in custom_ids:
            return f"任意パッドが重複しています: {custom_pad.catalog_id}"
        custom_ids.add(custom_pad.catalog_id)
        if message := validate_paste_flow_calibration_custom_pad(custom_pad):
            return message

    seen: set[str] = set()
    total_pad_count = 0
    for pattern in config.patterns:
        if not isinstance(pattern, PasteFlowCalibrationPattern):
            return "パッドパターンの形式が不正です"
        if not isinstance(pattern.catalog_id, str):
            return "パッドパターンIDは文字列で指定してください"
        is_custom_pad = is_custom_pad_catalog_id(pattern.catalog_id)
        if parse_pad_catalog_id(pattern.catalog_id) is None and not is_custom_pad:
            return "パッドパターンIDが不正です"
        if is_custom_pad and pattern.catalog_id not in custom_ids:
            return f"任意パッド定義がありません: {pattern.catalog_id}"
        if pattern.catalog_id in seen:
            return f"パッドパターンが重複しています: {pattern.catalog_id}"
        seen.add(pattern.catalog_id)
        span = pattern.rotation_span_deg
        if not is_finite_number(span) or span <= 0 or span > 360:
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
        remaining_pad_count = _MAX_CALIBRATION_PAD_COUNT - total_pad_count
        if pattern.rotation_count > remaining_pad_count // pattern.repeat_count:
            return "生成パッド総数は10,000以下で指定してください"
        total_pad_count += pattern.rotation_count * pattern.repeat_count
    return None


def validate_paste_flow_calibration_custom_pad(
    custom_pad: PasteFlowCalibrationCustomPadDraft | PasteFlowCalibrationCustomPadSpec,
) -> str | None:
    """任意寸法パッドを検証し、問題があれば説明を返す."""

    if not isinstance(
        custom_pad,
        PasteFlowCalibrationCustomPadDraft | PasteFlowCalibrationCustomPadSpec,
    ):
        return "任意パッドの形式が不正です"
    if not isinstance(custom_pad.name, str):
        return "任意パッドの名称は文字列で指定してください"
    name = custom_pad.name.strip()
    if isinstance(custom_pad, PasteFlowCalibrationCustomPadSpec) and not name:
        return "任意パッドの名称を入力してください"
    if len(name) > 120:
        return "任意パッドの名称は120文字以下で指定してください"
    if not is_paste_flow_calibration_custom_pad_shape_id(custom_pad.shape):
        return f"任意パッド形状が不正です: {custom_pad.shape}"
    shape = _CUSTOM_PAD_SHAPE_BY_ID[custom_pad.shape]
    if not is_finite_number(custom_pad.width_mm) or custom_pad.width_mm <= 0:
        return "任意パッドの幅／直径は正の有限値が必要です"
    if not is_kicad_length(custom_pad.width_mm, maximum_mm=_KICAD_MAX_PAD_SIZE_MM):
        return f"任意パッドの幅／直径は{_KICAD_PAD_SIZE_RANGE_TEXT}で指定してください"
    is_draft = isinstance(custom_pad, PasteFlowCalibrationCustomPadDraft)
    if shape.uses_height or not is_draft:
        if not is_finite_number(custom_pad.height_mm) or custom_pad.height_mm <= 0:
            return "任意パッドの高さは正の有限値が必要です"
        if not is_kicad_length(custom_pad.height_mm, maximum_mm=_KICAD_MAX_PAD_SIZE_MM):
            return f"任意パッドの高さは{_KICAD_PAD_SIZE_RANGE_TEXT}で指定してください"
    if (
        not is_draft
        and custom_pad.shape == "circle"
        and not math.isclose(custom_pad.width_mm, custom_pad.height_mm, abs_tol=1e-9)
    ):
        return "円パッドの幅と高さには同じ直径を指定してください"
    radius = custom_pad.corner_radius_mm
    if shape.uses_corner_radius:
        if not is_finite_number(radius):
            return "任意パッドの角丸半径は有限値が必要です"
        if radius <= 0:
            return "角丸矩形の角丸半径は0より大きい値が必要です"
        if not is_kicad_length(radius):
            return f"任意パッドの角丸半径は{_KICAD_LENGTH_RANGE_TEXT}で指定してください"
        if radius > min(custom_pad.width_mm, custom_pad.height_mm) / 2.0:
            return "角丸矩形の角丸半径は短辺の半分以下で指定してください"
    elif not is_draft:
        if not is_finite_number(radius):
            return "任意パッドの角丸半径は有限値が必要です"
        if radius != 0:
            return f"{shape.label}では角丸半径を指定できません"
    return None


def normalize_paste_flow_calibration_custom_pad_draft(
    draft: PasteFlowCalibrationCustomPadDraft,
) -> PasteFlowCalibrationCustomPadDraft:
    """形状に不要な寸法をコア側でcanonicalな値へ解決する."""

    if (message := validate_paste_flow_calibration_custom_pad(draft)) is not None:
        raise PasteFlowCalibrationBoardConfigError(message)
    shape = _CUSTOM_PAD_SHAPE_BY_ID[draft.shape]
    return attrs.evolve(
        draft,
        name=draft.name.strip(),
        height_mm=draft.height_mm if shape.uses_height else draft.width_mm,
        corner_radius_mm=(draft.corner_radius_mm if shape.uses_corner_radius else 0.0),
    )


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
        if is_custom_pad_catalog_id(pattern.catalog_id)
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
    return normalized_paste_flow_calibration_board_document(normalized)


def normalized_paste_flow_calibration_board_document(
    config: PasteFlowCalibrationBoardConfig,
) -> dict[str, Any]:
    return {
        "kind": PASTE_FLOW_CALIBRATION_BOARD_KIND,
        "schema_version": PASTE_FLOW_CALIBRATION_BOARD_SCHEMA_VERSION,
        "board": attrs.asdict(config.board),
        "purge_pad": attrs.asdict(config.purge_pad),
        "custom_pads": [attrs.asdict(item) for item in config.custom_pads],
        "patterns": [attrs.asdict(pattern) for pattern in config.patterns],
    }


def parse_paste_flow_calibration_board_document(
    document: Mapping[str, object],
) -> PasteFlowCalibrationBoardConfig | None:
    """JSON documentを設定へ変換する。不正な形式・値ではNoneを返す."""

    if not isinstance(document, Mapping) or not _has_exact_keys(
        document,
        (
            "kind",
            "schema_version",
            "board",
            "purge_pad",
            "custom_pads",
            "patterns",
        ),
    ):
        return None
    if document.get("kind") != PASTE_FLOW_CALIBRATION_BOARD_KIND:
        return None
    schema_version = document.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != PASTE_FLOW_CALIBRATION_BOARD_SCHEMA_VERSION
    ):
        return None
    board_data = document.get("board")
    purge_data = document.get("purge_pad")
    custom_pad_data = document.get("custom_pads")
    pattern_data = document.get("patterns")
    if not isinstance(board_data, Mapping) or not _has_exact_keys(
        board_data, ("width_mm", "height_mm", "edge_margin_mm", "pad_gap_mm")
    ):
        return None
    if not isinstance(purge_data, Mapping) or not _has_exact_keys(
        purge_data, ("width_mm", "height_mm")
    ):
        return None
    if not isinstance(custom_pad_data, list) or not isinstance(pattern_data, list):
        return None

    width = _document_float(board_data.get("width_mm"))
    height = _document_float(board_data.get("height_mm"))
    edge_margin = _document_float(board_data.get("edge_margin_mm"))
    pad_gap = _document_float(board_data.get("pad_gap_mm"))
    purge_width = _document_float(purge_data.get("width_mm"))
    purge_height = _document_float(purge_data.get("height_mm"))
    if (
        width is None
        or height is None
        or edge_margin is None
        or pad_gap is None
        or purge_width is None
        or purge_height is None
    ):
        return None

    custom_pads: list[PasteFlowCalibrationCustomPadSpec] = []
    for value in custom_pad_data:
        if not isinstance(value, Mapping) or not _has_exact_keys(
            value,
            (
                "catalog_id",
                "name",
                "shape",
                "width_mm",
                "height_mm",
                "corner_radius_mm",
            ),
        ):
            return None
        catalog_id = value.get("catalog_id")
        name = value.get("name")
        shape = value.get("shape")
        custom_width = _document_float(value.get("width_mm"))
        custom_height = _document_float(value.get("height_mm"))
        radius = _document_float(value.get("corner_radius_mm"))
        if not isinstance(catalog_id, str) or not isinstance(name, str):
            return None
        if not is_paste_flow_calibration_custom_pad_shape_id(shape):
            return None
        if custom_width is None or custom_height is None or radius is None:
            return None
        custom_pads.append(
            PasteFlowCalibrationCustomPadSpec(
                catalog_id=catalog_id,
                name=name,
                shape=shape,
                width_mm=custom_width,
                height_mm=custom_height,
                corner_radius_mm=radius,
            )
        )

    patterns: list[PasteFlowCalibrationPattern] = []
    for value in pattern_data:
        if not isinstance(value, Mapping) or not _has_exact_keys(
            value,
            (
                "catalog_id",
                "rotation_span_deg",
                "rotation_count",
                "repeat_count",
            ),
        ):
            return None
        catalog_id = value.get("catalog_id")
        span = _document_float(value.get("rotation_span_deg"))
        rotation_count = _document_int(value.get("rotation_count"))
        repeat_count = _document_int(value.get("repeat_count"))
        if not isinstance(catalog_id, str):
            return None
        if span is None or rotation_count is None or repeat_count is None:
            return None
        patterns.append(
            PasteFlowCalibrationPattern(
                catalog_id=catalog_id,
                rotation_span_deg=span,
                rotation_count=rotation_count,
                repeat_count=repeat_count,
            )
        )

    config = PasteFlowCalibrationBoardConfig(
        board=PasteFlowCalibrationBoardSpec(
            width_mm=width,
            height_mm=height,
            edge_margin_mm=edge_margin,
            pad_gap_mm=pad_gap,
        ),
        purge_pad=PasteFlowCalibrationPurgePadSpec(
            width_mm=purge_width,
            height_mm=purge_height,
        ),
        custom_pads=tuple(custom_pads),
        patterns=tuple(patterns),
    )
    if validate_paste_flow_calibration_board_config(config) is not None:
        return None
    return normalize_paste_flow_calibration_board_config(config)


def is_paste_flow_calibration_custom_pad_shape_id(
    value: object,
) -> TypeGuard[PasteFlowCalibrationCustomPadShapeId]:
    return isinstance(value, str) and value in _CUSTOM_PAD_SHAPE_BY_ID


def default_paste_flow_calibration_custom_pad_name(
    custom_pad: PasteFlowCalibrationCustomPadDraft,
) -> str:
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


def custom_pad_shape_option(
    shape: PasteFlowCalibrationCustomPadShapeId,
) -> PasteFlowCalibrationCustomPadShape:
    return _CUSTOM_PAD_SHAPE_BY_ID[shape]


def _has_exact_keys(value: Mapping[Any, Any], expected: tuple[str, ...]) -> bool:
    keys = tuple(value.keys())
    return len(keys) == len(expected) and all(
        isinstance(key, str) and key in expected for key in keys
    )


def _document_float(value: object) -> float | None:
    if not is_finite_number(value):
        return None
    return float(value)


def _document_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def parse_pad_catalog_id(catalog_id: object) -> tuple[str, str, int] | None:
    if not isinstance(catalog_id, str):
        return None
    footprint_id, separator, index_text = catalog_id.rpartition("#pad-")
    parsed = parse_footprint_id(footprint_id)
    if (
        not separator
        or parsed is None
        or not index_text.isdigit()
        or len(index_text) > 12
        or (len(index_text) > 1 and index_text.startswith("0"))
    ):
        return None
    return parsed[0], parsed[1], int(index_text)


_CUSTOM_PAD_ID_PATTERN = re.compile(r"custom:[0-9a-f]{32}")


def is_custom_pad_catalog_id(catalog_id: object) -> bool:
    return (
        isinstance(catalog_id, str)
        and _CUSTOM_PAD_ID_PATTERN.fullmatch(catalog_id) is not None
    )


def _pattern_sort_key(
    pattern: PasteFlowCalibrationPattern,
    custom_by_id: Mapping[str, PasteFlowCalibrationCustomPadSpec],
) -> tuple[object, ...]:
    parsed = parse_pad_catalog_id(pattern.catalog_id)
    if parsed is None:
        custom_pad = custom_by_id[pattern.catalog_id]
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
