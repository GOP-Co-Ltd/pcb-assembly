"""はんだペースト流量キャリブレーション基板の配置・生成.

標準 KiCad footprint の実 F.Cu / F.Paste 形状から配置 envelope を求め、
回転パターンを規則的な矩形グループとして基板へ並べる。WebUI はこのモジュールが 返す解決済み layout
を描画するだけで、配置規則を持たない。
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections import defaultdict
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
class PasteFlowCalibrationFootprint:
    """固定カタログの1 footprint."""

    catalog_id: str
    label: str
    family_id: str
    family_label: str
    library: str
    footprint: str
    reference_prefix: str
    default_rotation_span_deg: float
    default_rotation_count: int
    default_repeat_count: int
    initially_selected: bool = False


@attrs.frozen
class PasteFlowCalibrationBoardSpec:
    """基板外形と配置余白 [mm]."""

    width_mm: float = 40.0
    height_mm: float = 40.0
    edge_margin_mm: float = 1.0
    component_gap_mm: float = 1.0


@attrs.frozen
class PasteFlowCalibrationPurgePadSpec:
    """左上に置く専用 purge pad の寸法 [mm]."""

    width_mm: float = 2.0
    height_mm: float = 2.0


@attrs.frozen
class PasteFlowCalibrationPattern:
    """1 footprint の回転列 × 繰り返し行."""

    catalog_id: str
    rotation_span_deg: float = 180.0
    rotation_count: int = 4
    repeat_count: int = 3


def _default_patterns() -> tuple[PasteFlowCalibrationPattern, ...]:
    return tuple(
        PasteFlowCalibrationPattern(
            catalog_id=item.catalog_id,
            rotation_span_deg=item.default_rotation_span_deg,
            rotation_count=item.default_rotation_count,
            repeat_count=item.default_repeat_count,
        )
        for item in PASTE_FLOW_CALIBRATION_FOOTPRINTS
        if item.initially_selected
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
class PasteFlowCalibrationComponentLayout:
    """生成する1 footprint の解決済み配置."""

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
    family_id: str
    family_label: str
    bounds: PasteFlowCalibrationBounds
    cell_width_mm: float
    cell_height_mm: float
    angles_deg: tuple[float, ...]
    repeat_count: int
    components: tuple[PasteFlowCalibrationComponentLayout, ...]


@attrs.frozen
class PasteFlowCalibrationBoardLayout:
    """WebUIとKiCad生成が共有する完全に解決済みの配置."""

    board: PasteFlowCalibrationBoardSpec
    purge_pad: PasteFlowCalibrationBounds
    purge_polygons: tuple[PasteFlowCalibrationPolygon, ...]
    groups: tuple[PasteFlowCalibrationGroupLayout, ...]
    component_count: int


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


PASTE_FLOW_CALIBRATION_FOOTPRINTS: tuple[PasteFlowCalibrationFootprint, ...] = (
    PasteFlowCalibrationFootprint(
        "r_0402_1005metric",
        "0402",
        "chip_passive",
        "チップ受動部品",
        "Resistor_SMD.pretty",
        "R_0402_1005Metric",
        "R",
        180.0,
        4,
        3,
        True,
    ),
    PasteFlowCalibrationFootprint(
        "r_0603_1608metric",
        "0603",
        "chip_passive",
        "チップ受動部品",
        "Resistor_SMD.pretty",
        "R_0603_1608Metric",
        "R",
        180.0,
        4,
        3,
        True,
    ),
    PasteFlowCalibrationFootprint(
        "r_0805_2012metric",
        "0805",
        "chip_passive",
        "チップ受動部品",
        "Resistor_SMD.pretty",
        "R_0805_2012Metric",
        "R",
        180.0,
        4,
        3,
        True,
    ),
    PasteFlowCalibrationFootprint(
        "r_1206_3216metric",
        "1206",
        "chip_passive",
        "チップ受動部品",
        "Resistor_SMD.pretty",
        "R_1206_3216Metric",
        "R",
        180.0,
        4,
        3,
        True,
    ),
    PasteFlowCalibrationFootprint(
        "sot_23",
        "SOT-23",
        "sot",
        "SOT",
        "Package_TO_SOT_SMD.pretty",
        "SOT-23",
        "Q",
        360.0,
        4,
        2,
        True,
    ),
    PasteFlowCalibrationFootprint(
        "sot_23_5",
        "SOT-23-5",
        "sot",
        "SOT",
        "Package_TO_SOT_SMD.pretty",
        "SOT-23-5",
        "U",
        360.0,
        4,
        2,
        True,
    ),
    PasteFlowCalibrationFootprint(
        "soic_8",
        "SOIC-8",
        "small_outline",
        "Small outline",
        "Package_SO.pretty",
        "SOIC-8_3.9x4.9mm_P1.27mm",
        "U",
        180.0,
        4,
        2,
    ),
    PasteFlowCalibrationFootprint(
        "tssop_14",
        "TSSOP-14",
        "small_outline",
        "Small outline",
        "Package_SO.pretty",
        "TSSOP-14_4.4x5mm_P0.65mm",
        "U",
        180.0,
        4,
        2,
    ),
    PasteFlowCalibrationFootprint(
        "qfn_16_1ep",
        "QFN-16 EP",
        "no_lead",
        "No-lead",
        "Package_DFN_QFN.pretty",
        "QFN-16-1EP_3x3mm_P0.5mm_EP1.75x1.75mm",
        "U",
        360.0,
        4,
        2,
    ),
    PasteFlowCalibrationFootprint(
        "lqfp_32",
        "LQFP-32",
        "qfp",
        "QFP",
        "Package_QFP.pretty",
        "LQFP-32_7x7mm_P0.8mm",
        "U",
        360.0,
        4,
        1,
    ),
    PasteFlowCalibrationFootprint(
        "sot_223",
        "SOT-223",
        "power_smd",
        "Power SMD",
        "Package_TO_SOT_SMD.pretty",
        "SOT-223-3_TabPin2",
        "Q",
        360.0,
        4,
        1,
    ),
)

_CATALOG_BY_ID = {item.catalog_id: item for item in PASTE_FLOW_CALIBRATION_FOOTPRINTS}
_CATALOG_ORDER = {
    item.catalog_id: index
    for index, item in enumerate(PASTE_FLOW_CALIBRATION_FOOTPRINTS)
}


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
        "部品間余白": board.component_gap_mm,
    }
    for label, value in nonnegative.items():
        if not math.isfinite(value) or value < 0:
            return f"{label}は0以上の有限値が必要です"
    if board.width_mm <= 2 * board.edge_margin_mm:
        return "基板幅には左右の外周余白より大きい値が必要です"
    if board.height_mm <= 2 * board.edge_margin_mm:
        return "基板高さには上下の外周余白より大きい値が必要です"
    if not config.patterns:
        return "1つ以上の部品パターンが必要です"

    seen: set[str] = set()
    for pattern in config.patterns:
        if pattern.catalog_id not in _CATALOG_BY_ID:
            return f"未知の部品です: {pattern.catalog_id}"
        if pattern.catalog_id in seen:
            return f"部品が重複しています: {pattern.catalog_id}"
        seen.add(pattern.catalog_id)
        if (
            not math.isfinite(pattern.rotation_span_deg)
            or pattern.rotation_span_deg <= 0
            or pattern.rotation_span_deg > 360
        ):
            return "thetaは0より大きく360以下で指定してください"
        if (
            isinstance(pattern.rotation_count, bool)
            or not isinstance(pattern.rotation_count, int)
            or pattern.rotation_count < 1
        ):
            return "回転パターン数nは1以上の整数が必要です"
        if (
            isinstance(pattern.repeat_count, bool)
            or not isinstance(pattern.repeat_count, int)
            or pattern.repeat_count < 1
        ):
            return "繰り返し数mは1以上の整数が必要です"
    return None


def normalize_paste_flow_calibration_board_config(
    config: PasteFlowCalibrationBoardConfig,
) -> PasteFlowCalibrationBoardConfig:
    """パターンを固定カタログ順へ正規化する."""

    if (message := validate_paste_flow_calibration_board_config(config)) is not None:
        raise PasteFlowCalibrationBoardConfigError(message)
    return attrs.evolve(
        config,
        patterns=tuple(
            sorted(config.patterns, key=lambda item: _CATALOG_ORDER[item.catalog_id])
        ),
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
        "component_gap_mm",
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
            component_gap_mm=_document_float(board_data["component_gap_mm"]),
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
    """固定カタログからlayoutとKiCad基板を生成する公開サービス."""

    def __init__(self, footprint_root: Path | None = None) -> None:
        configured = os.environ.get("KICAD9_FOOTPRINT_DIR")
        self._footprint_root = (
            footprint_root
            if footprint_root is not None
            else Path(configured)
            if configured
            else DEFAULT_KICAD9_FOOTPRINT_DIR
        )

    @property
    def catalog(self) -> tuple[PasteFlowCalibrationFootprint, ...]:
        """固定された表示順のfootprintカタログ."""

        return PASTE_FLOW_CALIBRATION_FOOTPRINTS

    def layout(
        self, config: PasteFlowCalibrationBoardConfig
    ) -> PasteFlowCalibrationBoardLayout:
        """設定を検証し、実パッド形状を持つ配置へ解決する."""

        normalized = normalize_paste_flow_calibration_board_config(config)
        templates = {
            pattern.catalog_id: self._load_footprint(_CATALOG_BY_ID[pattern.catalog_id])
            for pattern in normalized.patterns
        }
        group_sizes = {
            pattern.catalog_id: self._group_metrics(
                pattern, templates[pattern.catalog_id]
            )
            for pattern in normalized.patterns
        }
        placements = self._pack_groups(normalized, group_sizes)
        counters: defaultdict[str, int] = defaultdict(int)
        groups: list[PasteFlowCalibrationGroupLayout] = []
        for pattern in normalized.patterns:
            item = _CATALOG_BY_ID[pattern.catalog_id]
            bounds = placements[pattern.catalog_id]
            cell_width, cell_height, angles, envelopes = group_sizes[pattern.catalog_id]
            components: list[PasteFlowCalibrationComponentLayout] = []
            for row in range(pattern.repeat_count):
                for column, angle in enumerate(angles):
                    envelope = envelopes[column]
                    cell_center_x = (
                        bounds.x
                        + column * (cell_width + normalized.board.component_gap_mm)
                        + cell_width / 2.0
                    )
                    cell_center_y = (
                        bounds.y
                        + row * (cell_height + normalized.board.component_gap_mm)
                        + cell_height / 2.0
                    )
                    anchor_x = cell_center_x - envelope.center_x
                    anchor_y = cell_center_y - envelope.center_y
                    counters[item.reference_prefix] += 1
                    reference = (
                        f"{item.reference_prefix}{counters[item.reference_prefix]}"
                    )
                    placed = _duplicate_footprint(templates[pattern.catalog_id])
                    placed.SetPosition(_vector(anchor_x, anchor_y))
                    placed.SetOrientationDegrees(angle)
                    components.append(
                        PasteFlowCalibrationComponentLayout(
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
                    family_id=item.family_id,
                    family_label=item.family_label,
                    bounds=bounds,
                    cell_width_mm=cell_width,
                    cell_height_mm=cell_height,
                    angles_deg=angles,
                    repeat_count=pattern.repeat_count,
                    components=tuple(components),
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
            component_count=sum(len(group.components) for group in groups),
        )

    def build_board(self, config: PasteFlowCalibrationBoardConfig) -> pcbnew.BOARD:
        """解決済みlayoutと同じ位置へ実footprintを置いたKiCad BOARDを返す."""

        layout = self.layout(config)
        board = generate_rect_pcb(layout.board.width_mm, layout.board.height_mm)
        self._add_purge_pad(board, layout.purge_pad)
        for group in layout.groups:
            item = _CATALOG_BY_ID[group.catalog_id]
            template = self._load_footprint(item)
            for component in group.components:
                footprint = _duplicate_footprint(template)
                footprint.SetReference(component.reference)
                footprint.SetValue(item.label)
                footprint.SetPosition(_vector(component.x, component.y))
                footprint.SetOrientationDegrees(component.rotation_deg)
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

        document = paste_flow_calibration_board_document(config)
        return (
            json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        )

    def _load_footprint(self, item: PasteFlowCalibrationFootprint) -> pcbnew.FOOTPRINT:
        library = self._footprint_root / item.library
        path = library / f"{item.footprint}.kicad_mod"
        if not path.is_file():
            raise PasteFlowCalibrationBoardEnvironmentError(
                f"KiCad footprintがありません: {path}"
            )
        footprint = pcbnew.FootprintLoad(str(library), item.footprint)
        if footprint is None:
            raise PasteFlowCalibrationBoardEnvironmentError(
                f"KiCad footprintを読み込めません: {path}"
            )
        return footprint

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
    ) -> dict[str, PasteFlowCalibrationBounds]:
        board = config.board
        left = board.edge_margin_mm
        right = board.width_mm - board.edge_margin_mm
        bottom = board.height_mm - board.edge_margin_mm
        y = board.edge_margin_mm + config.purge_pad.height_mm + board.component_gap_mm
        x = left
        row_height = 0.0
        family_id: str | None = None
        placements: dict[str, PasteFlowCalibrationBounds] = {}
        for pattern in config.patterns:
            item = _CATALOG_BY_ID[pattern.catalog_id]
            cell_width, cell_height, _angles, _envelopes = metrics[pattern.catalog_id]
            width = (
                pattern.rotation_count * cell_width
                + (pattern.rotation_count - 1) * board.component_gap_mm
            )
            height = (
                pattern.repeat_count * cell_height
                + (pattern.repeat_count - 1) * board.component_gap_mm
            )
            if width > right - left + 1e-9:
                raise PasteFlowCalibrationBoardOverflowError(
                    f"{item.label}のグループ幅{width:.2f} mmが配置可能幅"
                    f"{right - left:.2f} mmを超えます"
                )
            if family_id is not None and item.family_id != family_id:
                y += row_height + board.component_gap_mm
                x = left
                row_height = 0.0
            elif x > left and x + width > right + 1e-9:
                y += row_height + board.component_gap_mm
                x = left
                row_height = 0.0
            if y + height > bottom + 1e-9:
                raise PasteFlowCalibrationBoardOverflowError(
                    f"{item.label}を配置すると基板高さを超えます（必要下端"
                    f"{y + height:.2f} mm、配置可能下端{bottom:.2f} mm）"
                )
            placements[pattern.catalog_id] = PasteFlowCalibrationBounds(
                x=x, y=y, width=width, height=height
            )
            x += width + board.component_gap_mm
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
    "PASTE_FLOW_CALIBRATION_FOOTPRINTS",
    "PasteFlowCalibrationBoardConfig",
    "PasteFlowCalibrationBoardConfigError",
    "PasteFlowCalibrationBoardEnvironmentError",
    "PasteFlowCalibrationBoardGenerator",
    "PasteFlowCalibrationBoardLayout",
    "PasteFlowCalibrationBoardOverflowError",
    "PasteFlowCalibrationBoardSpec",
    "PasteFlowCalibrationBounds",
    "PasteFlowCalibrationComponentLayout",
    "PasteFlowCalibrationFootprint",
    "PasteFlowCalibrationGroupLayout",
    "PasteFlowCalibrationPattern",
    "PasteFlowCalibrationPoint",
    "PasteFlowCalibrationPolygon",
    "PasteFlowCalibrationPurgePadSpec",
    "normalize_paste_flow_calibration_board_config",
    "parse_paste_flow_calibration_board_document",
    "paste_flow_calibration_board_document",
    "validate_paste_flow_calibration_board_config",
]
