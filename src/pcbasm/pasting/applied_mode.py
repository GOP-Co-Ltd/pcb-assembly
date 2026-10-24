"""階層ノードごとの実際の塗布方式（auto 判定・フォールバック後）の集計.

pad 編集表の「塗布方式」列で、選択値から実際に塗る方式が読み取れないノードに
方式を添えるための算出。経路は作らず :meth:`FillPlan.mode_for_pad` で方式だけを判定する。
表示ラベルは :data:`PASTE_PARAM_FIELDS` の選択肢から引くので、方式が増えても
ここは直さずに済む。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

import attrs

from pcbasm.config.dispenser import PasteDispenserConfig
from pcbasm.pasting.fill_path import FillPlan
from pcbasm.pasting.params import PASTE_PARAM_FIELDS
from pcbasm.pasting.settings import ResolvedSetting
from pcbasm.pcb.grouping import HierKey, PadHierarchy, PadRef

# 方式の値 → 表示ラベル（選択肢の並び順 = 件数の表示順）
_MODE_LABELS: dict[str, str] = {
    choice.value: choice.label
    for field in PASTE_PARAM_FIELDS
    if field.name == "dispense_mode"
    for choice in field.choices
}


@attrs.frozen
class AppliedModeSummary:
    """1 ノード配下の有効 pad が実際に使う塗布方式.

    Attributes:
        counts: 方式ごとの有効 pad 数（選択肢の並び順。0 件の方式は含まない）
        label: 表示文字列（例: ``→ 面``、``→ 点 12 / 線 6``）
        title: ツールチップ用の説明
    """

    counts: dict[str, int]
    label: str
    title: str


def applied_mode_summaries(
    hierarchy: PadHierarchy,
    pad_settings: Mapping[PadRef, ResolvedSetting],
    node_settings: Mapping[HierKey, ResolvedSetting],
    config: PasteDispenserConfig,
) -> dict[HierKey, AppliedModeSummary]:
    """選択値から実際の方式が読み取れないノードについて、配下の有効 pad の方式を集計する.

    返すのは次のどちらかに当たるノードだけ。

    - 解決済みの方式が auto
    - 配下の有効 pad に、解決済みの方式と違う方式で塗る pad がある（フォールバックや子孫の override）

    無効 pad は塗らないので数えない。有効 pad が無いノードは返さない。

    Args:
        hierarchy: 基板の pad 階層
        pad_settings: :func:`~pcbasm.pasting.settings.resolve_pad_settings` の結果
        node_settings: :func:`~pcbasm.pasting.settings.resolve_node_settings` の結果
        config: ``MachineConfig.paste_dispenser``（ノズル径と auto 判定の閾値）
    """
    counts: dict[HierKey, Counter[str]] = {}
    for pad in hierarchy.iter_pads():
        setting = pad_settings[hierarchy.pad_ref_for_pad(pad)]
        if not setting.enabled:
            continue
        mode = FillPlan.mode_for_pad(pad.polygon, config=config, params=setting.params)
        for key in hierarchy.node_keys_for_pad(pad):
            counts.setdefault(key, Counter())[mode] += 1

    summaries: dict[HierKey, AppliedModeSummary] = {}
    for key, node_counts in counts.items():
        selected = node_settings[key].params.dispense_mode
        if selected == "auto" or set(node_counts) != {selected}:
            summaries[key] = _summary(node_counts)
    return summaries


def _summary(counts: Counter[str]) -> AppliedModeSummary:
    ordered = {mode: counts[mode] for mode in _MODE_LABELS if mode in counts}
    if len(ordered) == 1:
        label = f"→ {_MODE_LABELS[next(iter(ordered))]}"
    else:
        label = "→ " + " / ".join(
            f"{_MODE_LABELS[mode]} {n}" for mode, n in ordered.items()
        )
    detail = "、".join(f"{_MODE_LABELS[mode]} {n} 件" for mode, n in ordered.items())
    return AppliedModeSummary(
        counts=ordered,
        label=label,
        title=f"実際の塗布方式（有効 pad {sum(ordered.values())} 件）: {detail}",
    )
