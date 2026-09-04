"""塗布ジョブの装置非依存な前計画（対象 pad・順路・初回パージの解決）.

通常塗布と dataset 収集が、装置を動かす前に基板設定から「何をどの順に塗るか」を
決めるための純関数。エラーは ``(None, 理由)`` で返し、web ジョブが例外や 400 に変換する。
"""

from __future__ import annotations

from collections.abc import Mapping

import attrs

from pcbasm.pasting.initial_purge import (
    ResolvedInitialPurge,
    resolve_dataset_initial_purge,
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


@attrs.frozen
class DatasetTargets:
    """Dataset 収集の対象（purge pad と収集 pad の順路）."""

    hierarchy: PadHierarchy
    model: PasteSettingsModel
    resolved: Mapping[PadRef, ResolvedSetting]
    purge_pad: Pad
    purge_pad_id: str
    sample_pads: tuple[Pad, ...]

    @property
    def alignment_pads(self) -> tuple[Pad, ...]:
        return (*self.sample_pads, self.purge_pad)

    def params_for(self, pad: Pad) -> PasteParams:
        """収集 pad の解決済みパラメータ（収集 pad は全て階層内にあることを計画時に保証）."""
        return self.resolved[self.hierarchy.pad_ref_for_pad(pad)].params


def plan_dataset_targets(
    pcb: PcbFile,
    hierarchy: PadHierarchy,
    model: PasteSettingsModel,
    *,
    initial_purge_ul: float,
) -> tuple[DatasetTargets | None, str | None]:
    """任意 PCB から purge を除く有効 TOP pad の収集順路を装置非依存で解決する."""
    top_pads = [pad for pad in pcb.pads if pad.layer == Layer.TOP]
    resolved = resolve_pad_settings(hierarchy, model)
    purge, error = resolve_dataset_initial_purge(
        amount_ul=initial_purge_ul,
        pad_id=model.initial_purge_pad_id,
        hierarchy=hierarchy,
    )
    if error is not None:
        return None, error
    if purge is None:
        return None, "dataset収集には初回パージパッドが必要です"
    enabled = select_enabled_pads(top_pads, hierarchy, model)
    sample_pads = tuple(
        stop.pad
        for stop in plan_paste_route(pad for pad in enabled if pad is not purge.pad)
    )
    if not sample_pads:
        return None, "purge以外の収集対象padがありません"
    orphan_ids = [
        f"{pad.designator}.{pad.pad_number}"
        for pad in sample_pads
        if hierarchy.find_pad_id(pad) is None
    ]
    if orphan_ids:
        return None, (
            "dataset収集対象padに対応するComponentがありません: "
            + ", ".join(orphan_ids)
        )
    return (
        DatasetTargets(
            hierarchy=hierarchy,
            model=model,
            resolved=resolved,
            purge_pad=purge.pad,
            purge_pad_id=purge.pad_id,
            sample_pads=sample_pads,
        ),
        None,
    )


def _same_pad(a: Pad, b: Pad) -> bool:
    return a.designator == b.designator and a.pad_number == b.pad_number
