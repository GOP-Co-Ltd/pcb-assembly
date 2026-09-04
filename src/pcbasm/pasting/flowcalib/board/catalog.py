"""KiCad footprintの検索、パッド分類、任意寸法パッド生成."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from pathlib import Path

import attrs
import pcbnew

from pcbasm.pcb.footprint import (
    FRONT_PAD_LAYERS,
    FootprintEnvelope,
    FootprintInfo,
    FootprintLibrary,
    format_footprint_id,
    pad_geometry_signature,
    pad_on_any_layer,
    parse_footprint_id,
    search_tokens,
    single_pad_footprint,
    smd_pad_footprint,
)

from .config import (
    DEFAULT_FOOTPRINT_BY_SOURCE,
    BoardConfigError,
    CustomPadDraft,
    CustomPadShape,
    CustomPadSpec,
    format_pad_catalog_id,
    parse_pad_catalog_id,
)


@attrs.frozen
class PadPattern:
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


@attrs.frozen
class ResolvedPadPattern:
    """カタログ表示情報と生成用KiCad templateの組."""

    item: PadPattern
    template: pcbnew.FOOTPRINT


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


class PadCatalog:
    """Footprint indexと遅延ロードしたパッドtemplateを保持する."""

    def __init__(self, footprint_root: Path | None = None) -> None:
        self._library = FootprintLibrary(footprint_root)
        self._pad_patterns: dict[str, tuple[PadPattern, ...]] = {}
        self._pad_templates: dict[str, pcbnew.FOOTPRINT] = {}

    @property
    def footprint_count(self) -> int:
        return len(self._library.footprints)

    def search_footprints(
        self, query: str, limit: int = 30
    ) -> tuple[FootprintInfo, ...]:
        if not isinstance(query, str):
            raise BoardConfigError("footprint検索語は文字列で指定してください")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > 100
        ):
            raise BoardConfigError("footprint検索件数は1以上100以下で指定してください")
        if not search_tokens(query):
            by_id = {item.footprint_id: item for item in self._library.footprints}
            return tuple(
                by_id[format_footprint_id(library, footprint)]
                for library, footprint in _COMMON_FOOTPRINTS
                if format_footprint_id(library, footprint) in by_id
            )[:limit]
        return self._library.search(query, limit)

    def pad_patterns_for(self, footprint_id: str) -> tuple[PadPattern, ...]:
        cached = self._pad_patterns.get(footprint_id)
        if cached is not None:
            return cached
        parsed = parse_footprint_id(footprint_id)
        if parsed is None:
            raise BoardConfigError("footprint IDが不正です")
        library, footprint_name = parsed
        footprint = self._library.load(library, footprint_name)
        grouped: dict[tuple[object, ...], tuple[int, pcbnew.FOOTPRINT, list[str]]] = {}
        for pad_index, pad in enumerate(footprint.Pads()):
            if not pad_on_any_layer(pad, FRONT_PAD_LAYERS):
                continue
            template = single_pad_footprint(pad)
            signature = pad_geometry_signature(template)
            existing = grouped.get(signature)
            if existing is None:
                grouped[signature] = (pad_index, template, [pad.GetNumber()])
                continue
            existing[2].append(pad.GetNumber())
        if not grouped:
            raise BoardConfigError(
                f"F.Cu/F.Pasteパッドを持たないfootprintです: {footprint_id}"
            )

        family = library.removesuffix(".pretty")
        default = DEFAULT_FOOTPRINT_BY_SOURCE.get((library, footprint_name))
        patterns: list[PadPattern] = []
        for pad_index, template, numbers in sorted(
            grouped.values(), key=lambda item: item[0]
        ):
            envelope = FootprintEnvelope.measure(template, 0.0)
            catalog_id = format_pad_catalog_id(library, footprint_name, pad_index)
            source_numbers = _sorted_pad_numbers(numbers)
            item = PadPattern(
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
            )
            patterns.append(item)
            self._pad_templates[catalog_id] = template
        resolved = tuple(patterns)
        self._pad_patterns[footprint_id] = resolved
        return resolved

    def create_custom_pad(self, draft: CustomPadDraft) -> CustomPadSpec:
        draft = draft.normalized()
        return CustomPadSpec(
            catalog_id=f"custom:{uuid.uuid4().hex}",
            name=draft.name or draft.default_name(),
            shape=draft.shape,
            width_mm=draft.width_mm,
            height_mm=draft.height_mm,
            corner_radius_mm=draft.corner_radius_mm,
        )

    def resolve_pattern(
        self,
        catalog_id: str,
        custom_by_id: Mapping[str, CustomPadSpec],
    ) -> ResolvedPadPattern:
        custom_pad = custom_by_id.get(catalog_id)
        if custom_pad is None:
            return self._resolve_pad_pattern(catalog_id)
        template = smd_pad_footprint(
            custom_pad.shape,
            custom_pad.width_mm,
            custom_pad.height_mm,
            corner_radius_mm=custom_pad.corner_radius_mm,
        )
        envelope = FootprintEnvelope.measure(template, 0.0)
        shape = CustomPadShape.for_id(custom_pad.shape)
        radius = (
            f" · R{custom_pad.corner_radius_mm:.3g} mm"
            if custom_pad.shape == "roundrect"
            else ""
        )
        item = PadPattern(
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
        )
        return ResolvedPadPattern(item, template)

    def _resolve_pad_pattern(self, catalog_id: str) -> ResolvedPadPattern:
        parsed = parse_pad_catalog_id(catalog_id)
        if parsed is None:
            raise BoardConfigError("パッドパターンIDが不正です")
        library, footprint, _pad_index = parsed
        for item in self.pad_patterns_for(format_footprint_id(library, footprint)):
            if item.catalog_id == catalog_id:
                return ResolvedPadPattern(item, self._pad_templates[catalog_id])
        raise BoardConfigError(
            f"footprintに指定のパッドパターンがありません: {catalog_id}"
        )


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
