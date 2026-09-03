"""基板ごとの塗布設定モデルと L0–L4 階層 override の解決.

塗布実行と UI プレビューが同一の解決規則を共有するため、装置非依存の
純ロジックとしてここに集約する。

- :class:`PasteSettingsModel.base` が ``machine.toml`` 由来の全項目確定値
  （:class:`~pcbasm.pasting.params.PasteParams`）
- :class:`PasteSettingsModel.levels` が L0–L4 の疎な :class:`LevelSetting` 列
- 解決時は各 pad で L0→L4 を辿り、非 ``None`` 項目で上書きする（**最具体が勝つ**）。
  ``enabled`` も同じ規則で、明示の無い pad は有効

永続化（JSON 変換）は :mod:`pcbasm.pasting.persist`。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Self

import attrs

from pcbasm.config import PasteDispenser
from pcbasm.pasting.params import (
    PASTE_PARAM_NAMES,
    PasteParams,
    PasteParamsPatch,
    PasteParamValue,
)
from pcbasm.pcb.board import Pad
from pcbasm.pcb.grouping import HierKey, PadHierarchy, PadHierarchyNode, PadRef


@attrs.frozen
class LevelSetting:
    """1 階層ノードに紐づく明示設定.

    Attributes:
        key: 階層キー（L0–L4）
        enabled: 有効/無効（``None`` = 継承）
        patch: パラメータの差分上書き
    """

    key: HierKey
    enabled: bool | None = None
    patch: PasteParamsPatch = PasteParamsPatch()

    @property
    def is_empty(self) -> bool:
        return self.enabled is None and self.patch.is_empty


@attrs.frozen
class ResolvedSetting:
    """1 pad / 1 ノードに対する解決済み設定."""

    enabled: bool
    params: PasteParams


@attrs.frozen
class PasteSettingsModel:
    """基板 1 枚分の塗布設定モデル.

    Attributes:
        base: machine.toml 由来のデフォルト（全項目確定）
        initial_purge_pad_id: 初回パージに使う pad id（``None`` = 順路先頭）
        levels: L0–L4 の疎な明示設定（キーは重複しない）
    """

    base: PasteParams
    initial_purge_pad_id: str | None = None
    levels: tuple[LevelSetting, ...] = ()

    @classmethod
    def from_config(cls, config: PasteDispenser) -> Self:
        """Override の無い初期モデル（machine.toml デフォルトのみ）."""
        return cls(base=PasteParams.from_config(config))

    def level(self, key: HierKey) -> LevelSetting | None:
        for setting in self.levels:
            if setting.key == key:
                return setting
        return None

    def with_level_patch(
        self,
        key: HierKey,
        *,
        values: Mapping[str, PasteParamValue] | None = None,
        clear: Sequence[str] = (),
        enabled: bool | None = None,
        enabled_sent: bool = False,
    ) -> Self:
        """1 ノードの明示設定を upsert / clear した新しいモデルを返す.

        - ``values`` の項目で patch を上書きし、``clear`` の項目を継承に戻す
        - ``enabled_sent`` が False のときは既存の ``enabled`` を保持する
        - 結果が空（``enabled`` も patch も無し）ならそのノードを ``levels`` から除く
        """
        current = self.level(key) or LevelSetting(key)
        updated = LevelSetting(
            key,
            enabled=enabled if enabled_sent else current.enabled,
            patch=current.patch.updated(values, clear=clear),
        )
        if updated.is_empty:
            levels = tuple(s for s in self.levels if s.key != key)
        elif self.level(key) is None:
            levels = (*self.levels, updated)
        else:
            levels = tuple(updated if s.key == key else s for s in self.levels)
        return attrs.evolve(self, levels=levels)

    def with_pads_enabled(self, l4_keys: Iterable[HierKey], *, enabled: bool) -> Self:
        """指定 L4 ノード群の ``enabled`` を一括設定した新モデルを返す."""
        model = self
        for key in l4_keys:
            model = model.with_level_patch(key, enabled=enabled, enabled_sent=True)
        return model

    def with_initial_purge_pad_id(self, pad_id: str | None) -> Self:
        return attrs.evolve(self, initial_purge_pad_id=pad_id)

    def without_levels(self, keys: Iterable[HierKey]) -> Self:
        """指定キーの明示設定を除いた新モデルを返す."""
        drop = set(keys)
        return attrs.evolve(
            self, levels=tuple(s for s in self.levels if s.key not in drop)
        )


def _resolve_chain(
    keys: Iterable[HierKey], model: PasteSettingsModel
) -> ResolvedSetting:
    """L0 から最具体へ並んだ ``keys`` の明示設定を順に重ねて解決する."""
    params = model.base
    enabled = True
    for key in keys:
        setting = model.level(key)
        if setting is None:
            continue
        params = params.patched(setting.patch)
        if setting.enabled is not None:
            enabled = setting.enabled
    return ResolvedSetting(enabled=enabled, params=params)


def resolve_pad_settings(
    hierarchy: PadHierarchy, model: PasteSettingsModel
) -> dict[PadRef, ResolvedSetting]:
    """各 pad の解決済み設定を返す（``hierarchy.node_keys_for_pad`` の L0→L4 順に重ねる）."""
    return {
        hierarchy.pad_ref_for_pad(pad): _resolve_chain(
            hierarchy.node_keys_for_pad(pad), model
        )
        for pad in hierarchy.iter_pads()
    }


def resolve_node_settings(
    hierarchy: PadHierarchy, model: PasteSettingsModel
) -> dict[HierKey, ResolvedSetting]:
    """各階層ノードの解決済み設定を返す（ルートから自ノードまでのパスで重ねる）.

    UI の階層表が各ノード行に解決済み値を表示するための算出。
    """
    result: dict[HierKey, ResolvedSetting] = {}
    for path in _node_paths(hierarchy.root, ()):
        result[path[-1]] = _resolve_chain(path, model)
    return result


def _node_paths(
    node: PadHierarchyNode, ancestors: tuple[HierKey, ...]
) -> Iterator[tuple[HierKey, ...]]:
    path = (*ancestors, node.key)
    yield path
    for child in node.children:
        yield from _node_paths(child, path)


def is_pad_enabled(
    pad: Pad, hierarchy: PadHierarchy, resolved: Mapping[PadRef, ResolvedSetting]
) -> bool:
    """Pad が塗布対象か判定する.

    階層から除外された pad（対応 Component 無し = ``resolved`` に不在）は
    後方互換で有効扱い、それ以外は解決済み ``enabled`` に従う。
    """
    pad_ref = hierarchy.find_pad_ref(pad)
    if pad_ref is None:
        return True
    setting = resolved.get(pad_ref)
    return setting is None or setting.enabled


def select_enabled_pads(
    pads: Iterable[Pad], hierarchy: PadHierarchy, model: PasteSettingsModel
) -> list[Pad]:
    """塗布対象（enabled）の pad だけを元の順序で返す.

    プレビュー（webui router）と実行（webui job）が同一の絞り込みを共有するための 単一ソース。
    """
    resolved = resolve_pad_settings(hierarchy, model)
    return [pad for pad in pads if is_pad_enabled(pad, hierarchy, resolved)]


def find_orphans(model: PasteSettingsModel, hierarchy: PadHierarchy) -> list[HierKey]:
    """現階層に存在しない設定キー（孤児）を返す."""
    existing = hierarchy.all_keys()
    return [setting.key for setting in model.levels if setting.key not in existing]


@attrs.frozen
class OwnOverrideSummary:
    """ノード自身の明示設定の集計（UI の行バッジ表示用）.

    Attributes:
        enabled: enabled を明示しているか
        fields: 上書きされたフィールド名（UI 列順）
        count: fields + enabled の総数
    """

    enabled: bool
    fields: tuple[str, ...]
    count: int


@attrs.frozen
class DescendantOverrideSummary:
    """子孫ノード（自身は含まない）の明示設定の集計（UI の継承マーカー表示用）.

    Attributes:
        enabled_count: enabled を明示した子孫ノード数
        field_counts: フィールドごとの子孫 override 数（UI 列順、0 も含む）
        fields: 子孫に override があるフィールド名（UI 列順）
        node_count: 明示設定を持つ子孫ノード数
        count: 子孫の明示項目の総数
    """

    enabled_count: int
    field_counts: dict[str, int]
    fields: tuple[str, ...]
    node_count: int
    count: int


def own_override_summary(setting: LevelSetting | None) -> OwnOverrideSummary:
    """ノード自身の明示設定を集計する（継承は数えない）."""
    enabled = setting is not None and setting.enabled is not None
    fields = tuple(setting.patch.to_dict()) if setting is not None else ()
    return OwnOverrideSummary(
        enabled=enabled, fields=fields, count=len(fields) + (1 if enabled else 0)
    )


def descendant_override_summary(
    node: PadHierarchyNode, model: PasteSettingsModel
) -> DescendantOverrideSummary:
    """``node`` の子孫（自身は含めない）の明示設定を集計する."""
    field_counts = {name: 0 for name in PASTE_PARAM_NAMES}
    enabled_count = 0
    node_count = 0
    total = 0
    for descendant in _iter_descendants(node):
        setting = model.level(descendant.key)
        if setting is None or setting.is_empty:
            continue
        own = own_override_summary(setting)
        if own.enabled:
            enabled_count += 1
        for name in own.fields:
            field_counts[name] += 1
        total += own.count
        node_count += 1
    return DescendantOverrideSummary(
        enabled_count=enabled_count,
        field_counts=field_counts,
        fields=tuple(name for name in PASTE_PARAM_NAMES if field_counts[name] > 0),
        node_count=node_count,
        count=total,
    )


def _iter_descendants(node: PadHierarchyNode) -> Iterator[PadHierarchyNode]:
    for child in node.children:
        yield child
        yield from _iter_descendants(child)
