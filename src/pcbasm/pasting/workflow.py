"""塗布ジョブの装置非依存な前計画（対象 pad・順路・初回パージの解決）.

通常塗布が、装置を動かす前に基板設定から「何をどの順に塗るか」を決めるための純関数。
エラーは ``(None, 理由)`` で返し、web ジョブが例外や 400 に変換する。
"""

from __future__ import annotations

from collections.abc import Mapping

import attrs

from pcbasm.pasting.initial_purge import (
    ResolvedInitialPurge,
    resolve_initial_purge,
)
from pcbasm.pasting.params import PasteParams
from pcbasm.pasting.route import plan_paste_route
from pcbasm.pasting.settings import (
    PasteSettingsModel,
    ResolvedSetting,
    resolve_pad_settings,
    select_enabled_pads,
)
from pcbasm.pcb import Layer, Pad, PadHierarchy, PadRef, PcbFile


@attrs.frozen
class PasteTargets:
    """通常塗布の対象と順路.

    Attributes:
        hierarchy: pad 階層
        model: 基板の塗布設定モデル
        resolved: pad ごとの解決済み設定
        top_pads: 対象レイヤの全 pad（有効/無効問わず）
        routed_pads: 有効 pad の塗布順路
        initial_purge: 初回パージ（無効なら ``None``）
    """

    hierarchy: PadHierarchy
    model: PasteSettingsModel
    resolved: Mapping[PadRef, ResolvedSetting]
    top_pads: tuple[Pad, ...]
    routed_pads: tuple[Pad, ...]
    initial_purge: ResolvedInitialPurge | None

    @property
    def disabled_count(self) -> int:
        return len(self.top_pads) - len(self.routed_pads)

    @property
    def alignment_pads(self) -> tuple[Pad, ...]:
        """位置合わせに使う pad（順路 + 初回パージ pad が順路外ならそれも）."""
        if self.initial_purge is None or any(
            _same_pad(pad, self.initial_purge.pad) for pad in self.routed_pads
        ):
            return self.routed_pads
        return (*self.routed_pads, self.initial_purge.pad)

    def params_for(self, pad: Pad) -> PasteParams | None:
        """Pad の解決済みパラメータ。階層外（対応 Component 無し）の pad は ``None``."""
        pad_ref = self.hierarchy.find_pad_ref(pad)
        if pad_ref is None:
            return None
        setting = self.resolved.get(pad_ref)
        return None if setting is None else setting.params


def plan_paste_targets(
    pcb: PcbFile,
    hierarchy: PadHierarchy,
    model: PasteSettingsModel,
    *,
    initial_purge_ul: float,
    layer: Layer = Layer.TOP,
) -> tuple[PasteTargets | None, str | None]:
    """基板設定から通常塗布の対象 pad・順路・初回パージを解決する."""
    top_pads = tuple(pad for pad in pcb.pads if pad.layer == layer)
    resolved = resolve_pad_settings(hierarchy, model)
    routed = tuple(
        stop.pad
        for stop in plan_paste_route(select_enabled_pads(top_pads, hierarchy, model))
    )
    initial_purge, error = resolve_initial_purge(
        amount_ul=initial_purge_ul,
        pad_id=model.initial_purge_pad_id,
        hierarchy=hierarchy,
        routed_pads=routed,
        layer=layer,
    )
    if error is not None:
        return None, error
    return (
        PasteTargets(
            hierarchy=hierarchy,
            model=model,
            resolved=resolved,
            top_pads=top_pads,
            routed_pads=routed,
            initial_purge=initial_purge,
        ),
        None,
    )


def _same_pad(a: Pad, b: Pad) -> bool:
    return a.designator == b.designator and a.pad_number == b.pad_number
