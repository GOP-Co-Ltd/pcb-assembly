"""KiCad footprintの検索、パッド分類、任意寸法パッド生成."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import attrs
import pcbnew

from .config import (
    DEFAULT_FOOTPRINT_BY_SOURCE,
    KICAD_VECTOR_COORDINATE_MAX_NM,
    KICAD_VECTOR_COORDINATE_MIN_NM,
    PasteFlowCalibrationBoardConfigError,
    PasteFlowCalibrationBoardEnvironmentError,
    PasteFlowCalibrationCustomPadDraft,
    PasteFlowCalibrationCustomPadSpec,
    custom_pad_shape_option,
    default_paste_flow_calibration_custom_pad_name,
    format_footprint_id,
    format_pad_catalog_id,
    normalize_paste_flow_calibration_custom_pad_draft,
    parse_footprint_id,
    parse_pad_catalog_id,
)

DEFAULT_KICAD9_FOOTPRINT_DIR = Path("/usr/share/kicad/footprints")


@attrs.frozen
class PasteFlowCalibrationFootprintInfo:
    """検索可能なKiCad footprint."""

    footprint_id: str
    label: str
    library: str
    footprint: str


@attrs.frozen
class PasteFlowCalibrationPadPattern:
    """1 footprint内で回転同値なパッドをまとめたカタログ項目."""

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
class PasteFlowCalibrationResolvedPadPattern:
    """カタログ表示情報と生成用KiCad templateの組."""

    item: PasteFlowCalibrationPadPattern
    template: pcbnew.FOOTPRINT


@attrs.frozen
class PasteFlowCalibrationFootprintEnvelope:
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


# 空検索時に列挙する、はんだペースト印刷で一般的なSMD footprint。
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
    (
        "Package_DFN_QFN.pretty",
        "QFN-16-1EP_3x3mm_P0.5mm_EP1.75x1.75mm",
    ),
    (
        "Package_DFN_QFN.pretty",
        "QFN-24-1EP_4x4mm_P0.5mm_EP2.5x2.5mm",
    ),
    (
        "Package_DFN_QFN.pretty",
        "QFN-32-1EP_5x5mm_P0.5mm_EP3.1x3.1mm",
    ),
    (
        "Package_DFN_QFN.pretty",
        "QFN-48-1EP_7x7mm_P0.5mm_EP5.15x5.15mm",
    ),
    ("Package_QFP.pretty", "LQFP-32_7x7mm_P0.8mm"),
    ("Package_QFP.pretty", "LQFP-48_7x7mm_P0.5mm"),
    ("Package_QFP.pretty", "LQFP-64_10x10mm_P0.5mm"),
    ("Package_QFP.pretty", "LQFP-100_14x14mm_P0.5mm"),
    ("Crystal.pretty", "Crystal_SMD_2012-2Pin_2.0x1.2mm"),
    ("Crystal.pretty", "Crystal_SMD_2520-4Pin_2.5x2.0mm"),
    ("Crystal.pretty", "Crystal_SMD_3225-4Pin_3.2x2.5mm"),
    ("Crystal.pretty", "Crystal_SMD_5032-4Pin_5.0x3.2mm"),
)


class PasteFlowCalibrationPadCatalog:
    """Footprint indexと遅延ロードしたパッドtemplateを保持する."""

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
    def footprint_count(self) -> int:
        return len(self._indexed_footprints())

    def search_footprints(
        self, query: str, limit: int = 30
    ) -> tuple[PasteFlowCalibrationFootprintInfo, ...]:
        if not isinstance(query, str):
            raise PasteFlowCalibrationBoardConfigError(
                "footprint検索語は文字列で指定してください"
            )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > 100
        ):
            raise PasteFlowCalibrationBoardConfigError(
                "footprint検索件数は1以上100以下で指定してください"
            )
        footprints = self._indexed_footprints()
        tokens = _search_tokens(query)
        if not tokens:
            by_id = {item.footprint_id: item for item in footprints}
            return tuple(
                by_id[format_footprint_id(library, footprint)]
                for library, footprint in _COMMON_FOOTPRINTS
                if format_footprint_id(library, footprint) in by_id
            )[:limit]
        matches: list[PasteFlowCalibrationFootprintInfo] = []
        for item in footprints:
            search_text = _search_text(item)
            if all(token in search_text for token in tokens):
                matches.append(item)
        normalized_query = " ".join(tokens)
        matches.sort(key=lambda item: _search_rank(item, normalized_query))
        return tuple(matches[:limit])

    def pad_patterns_for(
        self, footprint_id: str
    ) -> tuple[PasteFlowCalibrationPadPattern, ...]:
        cached = self._pad_patterns.get(footprint_id)
        if cached is not None:
            return cached
        parsed = parse_footprint_id(footprint_id)
        if parsed is None:
            raise PasteFlowCalibrationBoardConfigError("footprint IDが不正です")
        library, footprint_name = parsed
        footprint = self._load_footprint(library, footprint_name)
        grouped: dict[tuple[object, ...], tuple[int, pcbnew.FOOTPRINT, list[str]]] = {}
        for pad_index, pad in enumerate(footprint.Pads()):
            if not _has_relevant_layer(pad):
                continue
            template = _single_pad_footprint(pad)
            signature = _pad_geometry_signature(template)
            existing = grouped.get(signature)
            if existing is None:
                grouped[signature] = (pad_index, template, [pad.GetNumber()])
                continue
            _representative_index, _representative, numbers = existing
            numbers.append(pad.GetNumber())
        if not grouped:
            raise PasteFlowCalibrationBoardConfigError(
                f"F.Cu/F.Pasteパッドを持たないfootprintです: {footprint_id}"
            )

        family = library.removesuffix(".pretty")
        default = DEFAULT_FOOTPRINT_BY_SOURCE.get((library, footprint_name))
        patterns: list[PasteFlowCalibrationPadPattern] = []
        for pad_index, template, numbers in sorted(
            grouped.values(), key=lambda item: item[0]
        ):
            envelope = footprint_envelope(template, 0.0)
            catalog_id = format_pad_catalog_id(library, footprint_name, pad_index)
            source_numbers = _sorted_pad_numbers(numbers)
            item = PasteFlowCalibrationPadPattern(
                catalog_id=catalog_id,
                footprint_id=footprint_id,
                footprint_label=footprint_name,
                label=_pad_pattern_label(
                    source_numbers, len(numbers), envelope.width, envelope.height
                ),
                family_id=library,
                family_label=family,
                library=library,
                footprint=footprint_name,
                source_pad_numbers=source_numbers,
                source_pad_count=len(numbers),
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

    def create_custom_pad(
        self, draft: PasteFlowCalibrationCustomPadDraft
    ) -> PasteFlowCalibrationCustomPadSpec:
        draft = normalize_paste_flow_calibration_custom_pad_draft(draft)
        return PasteFlowCalibrationCustomPadSpec(
            catalog_id=f"custom:{uuid.uuid4().hex}",
            name=draft.name or default_paste_flow_calibration_custom_pad_name(draft),
            shape=draft.shape,
            width_mm=draft.width_mm,
            height_mm=draft.height_mm,
            corner_radius_mm=draft.corner_radius_mm,
        )

    def resolve_pattern(
        self,
        catalog_id: str,
        custom_by_id: Mapping[str, PasteFlowCalibrationCustomPadSpec],
    ) -> PasteFlowCalibrationResolvedPadPattern:
        custom_pad = custom_by_id.get(catalog_id)
        if custom_pad is None:
            return self._resolve_pad_pattern(catalog_id)
        template = _custom_pad_footprint(custom_pad)
        envelope = footprint_envelope(template, 0.0)
        shape = custom_pad_shape_option(custom_pad.shape)
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
                f"{shape.label} · {envelope.width:.3g} × "
                f"{envelope.height:.3g} mm{radius}"
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
        return PasteFlowCalibrationResolvedPadPattern(item, template)

    def _indexed_footprints(
        self,
    ) -> tuple[PasteFlowCalibrationFootprintInfo, ...]:
        if self._footprint_index is not None:
            return self._footprint_index
        try:
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
                            footprint_id=format_footprint_id(library, footprint),
                            label=f"{family} / {footprint}",
                            library=library,
                            footprint=footprint,
                        )
                    )
        except OSError as error:
            raise PasteFlowCalibrationBoardEnvironmentError(
                f"KiCad footprint rootを確認できません: {self._footprint_root}"
            ) from error
        if not footprints:
            raise PasteFlowCalibrationBoardEnvironmentError(
                f"KiCad footprintがありません: {self._footprint_root}"
            )
        self._footprint_index = tuple(footprints)
        return self._footprint_index

    def _load_footprint(self, library: str, footprint: str) -> pcbnew.FOOTPRINT:
        library_path = self._footprint_root / library
        path = library_path / f"{footprint}.kicad_mod"
        try:
            is_file = path.is_file()
        except OSError as error:
            raise PasteFlowCalibrationBoardEnvironmentError(
                f"KiCad footprintを確認できません: {path}"
            ) from error
        if not is_file:
            raise PasteFlowCalibrationBoardEnvironmentError(
                f"KiCad footprintがありません: {path}"
            )
        try:
            loaded = pcbnew.FootprintLoad(str(library_path), footprint)
        except OSError as error:
            raise PasteFlowCalibrationBoardEnvironmentError(
                f"KiCad footprintを読み込めません: {path}"
            ) from error
        if loaded is None:
            raise PasteFlowCalibrationBoardEnvironmentError(
                f"KiCad footprintを読み込めません: {path}"
            )
        return loaded

    def _resolve_pad_pattern(
        self, catalog_id: str
    ) -> PasteFlowCalibrationResolvedPadPattern:
        parsed = parse_pad_catalog_id(catalog_id)
        if parsed is None:
            raise PasteFlowCalibrationBoardConfigError("パッドパターンIDが不正です")
        library, footprint, _pad_index = parsed
        for item in self.pad_patterns_for(format_footprint_id(library, footprint)):
            if item.catalog_id == catalog_id:
                return PasteFlowCalibrationResolvedPadPattern(
                    item, self._pad_templates[catalog_id]
                )
        raise PasteFlowCalibrationBoardConfigError(
            f"footprintに指定のパッドパターンがありません: {catalog_id}"
        )


def _search_tokens(query: str) -> tuple[str, ...]:
    return tuple(
        token for token in re.split(r"[\s_:/.-]+", query.casefold().strip()) if token
    )


def _search_text(item: PasteFlowCalibrationFootprintInfo) -> str:
    return " ".join(
        _search_tokens(f"{item.library.removesuffix('.pretty')} {item.footprint}")
    )


def _search_rank(
    item: PasteFlowCalibrationFootprintInfo, normalized_query: str
) -> tuple[object, ...]:
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
    duplicated.SetPosition(vector(0.0, 0.0))
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
    pad.SetSize(vector(custom_pad.width_mm, custom_pad.height_mm))
    pad.SetPosition(vector(0.0, 0.0))
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
_LAYOUT_LAYERS = (pcbnew.F_Cu, pcbnew.F_Paste)
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
    rotated = duplicate_footprint(footprint)
    rotated.SetPosition(vector(0.0, 0.0))
    rotated.SetOrientationDegrees(angle)
    pad = next(iter(rotated.Pads()))
    raw: list[tuple[str, tuple[tuple[int, int], ...]]] = []
    all_points: list[tuple[int, int]] = []
    for layer_name, layer in _SIGNATURE_LAYERS:
        if not pad.GetLayerSet().Contains(layer):
            continue
        shape = effective_pad_polygon(pad, layer)
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
        if value.isdigit() and len(value) <= 12:
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
    elif all(value.isdigit() and len(value) <= 12 for value in numbered):
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


def footprint_envelope(
    footprint: pcbnew.FOOTPRINT, angle: float
) -> PasteFlowCalibrationFootprintEnvelope:
    placed = duplicate_footprint(footprint)
    placed.SetPosition(vector(0.0, 0.0))
    placed.SetOrientationDegrees(angle)
    bounds: list[tuple[float, float, float, float]] = []
    for pad in placed.Pads():
        for layer in _LAYOUT_LAYERS:
            if not pad.GetLayerSet().Contains(layer):
                continue
            shape = effective_pad_polygon(pad, layer)
            if shape.OutlineCount() < 1:
                continue
            box = shape.BBox()
            min_x = to_mm(box.GetX())
            min_y = to_mm(box.GetY())
            bounds.append(
                (
                    min_x,
                    min_y,
                    min_x + to_mm(box.GetWidth()),
                    min_y + to_mm(box.GetHeight()),
                )
            )
    if not bounds:
        raise PasteFlowCalibrationBoardEnvironmentError(
            "footprintにF.Cu/F.Pasteパッドがありません"
        )
    return PasteFlowCalibrationFootprintEnvelope(
        min(item[0] for item in bounds),
        min(item[1] for item in bounds),
        max(item[2] for item in bounds),
        max(item[3] for item in bounds),
    )


def duplicate_footprint(footprint: pcbnew.FOOTPRINT) -> pcbnew.FOOTPRINT:
    duplicated = footprint.Duplicate()
    if duplicated is None:
        raise PasteFlowCalibrationBoardEnvironmentError(
            "KiCad footprintを複製できません"
        )
    return duplicated


def effective_pad_polygon(pad: pcbnew.PAD, layer: int) -> pcbnew.SHAPE_POLY_SET:
    """KiCad内部座標で表現可能なpad polygonを返す."""

    try:
        shape = pad.GetEffectivePolygon(layer)
        box = shape.BBox()
    except OverflowError as error:
        raise PasteFlowCalibrationBoardEnvironmentError(
            "footprintのパッド形状がKiCadの座標範囲を超えています"
        ) from error
    x = box.GetX()
    y = box.GetY()
    width = box.GetWidth()
    height = box.GetHeight()
    if (
        width <= 0
        or height <= 0
        or width > KICAD_VECTOR_COORDINATE_MAX_NM
        or height > KICAD_VECTOR_COORDINATE_MAX_NM
        or x < KICAD_VECTOR_COORDINATE_MIN_NM
        or y < KICAD_VECTOR_COORDINATE_MIN_NM
        or x + width > KICAD_VECTOR_COORDINATE_MAX_NM
        or y + height > KICAD_VECTOR_COORDINATE_MAX_NM
    ):
        raise PasteFlowCalibrationBoardEnvironmentError(
            "footprintのパッド形状がKiCadの座標範囲を超えています"
        )
    return shape


def vector(x: float, y: float) -> pcbnew.VECTOR2I:
    coordinates = (from_mm(x), from_mm(y))
    if any(
        value < KICAD_VECTOR_COORDINATE_MIN_NM or value > KICAD_VECTOR_COORDINATE_MAX_NM
        for value in coordinates
    ):
        raise PasteFlowCalibrationBoardEnvironmentError(
            "footprint形状から算出した位置がKiCadの座標範囲を超えています"
        )
    return pcbnew.VECTOR2I(*coordinates)


def from_mm(value: float) -> int:
    return cast(int, pcbnew.FromMM(value))


def to_mm(value: int | float) -> float:
    return float(value) / 1_000_000.0
