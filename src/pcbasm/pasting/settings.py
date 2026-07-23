"""塗布設定の override モデルと階層解決.

塗布実行（``PasteApplicator``）と UI プレビューが同一の解決規則を
共有するため、装置非依存の純ロジックとしてここに集約する。

設定モデルは override 思想:

- :class:`PasteOverride` の各項目は ``None`` = 継承（上位レベルの値を使う）
- :class:`PasteSettingsModel.base` が machine.toml 由来の全項目確定デフォルト
- :class:`PasteSettingsModel.levels` が L0–L4 の疎マップ
- 解決時は各 pad で L0→L4 を辿り、非 None 項目で上書き（**最具体が勝つ**）
"""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

import attrs

from pcbasm.config import DISPENSE_MODES, DispenseMode, PasteDispenser, PasteHeight
from pcbasm.pcb.board import Pad
from pcbasm.pcb.grouping import HierKey, PadHierarchy, PadHierarchyNode, PadRef

# override 可能な項目のフィールド名（解決・JSON 変換の正準順）
PASTE_OVERRIDE_FIELDS: tuple[str, ...] = (
    "dispense_mode",
    "paste_height",
    "ul_per_mm2",
    "prime_extra_delay",
    "bead_width_factor",
    "overlap",
    "boundary_margin",
)
NUMERIC_PASTE_OVERRIDE_FIELDS: tuple[str, ...] = (
    "ul_per_mm2",
    "prime_extra_delay",
    "bead_width_factor",
    "overlap",
    "boundary_margin",
)

# 有効/無効の3値（True / False / None=継承）
EnableState = bool | None
PasteSettingValue = float | str


@attrs.frozen
class PasteOverride:
    """塗布パラメータの差分上書き（``None`` = 継承）.

    ``None`` の項目は上位レベルの値を継承する（JSON では欠落で表現）。
    """

    dispense_mode: DispenseMode | None = None
    paste_height: PasteHeight | None = None
    ul_per_mm2: float | None = None
    prime_extra_delay: float | None = None
    bead_width_factor: float | None = None
    overlap: float | None = None
    boundary_margin: float | None = None

    def __attrs_post_init__(self) -> None:
        if self.dispense_mode is not None and self.dispense_mode not in DISPENSE_MODES:
            raise ValueError(f"未知の塗布方式です: {self.dispense_mode}")
        if self.paste_height is not None:
            _check_paste_height(self.paste_height)
        for field in NUMERIC_PASTE_OVERRIDE_FIELDS:
            _check_numeric(field, getattr(self, field))


@attrs.frozen
class LevelSetting:
    """1階層ノードに紐づく設定.

    Attributes:
        enabled: 有効/無効（``None`` = 継承）
        override: パラメータの差分上書き
    """

    enabled: bool | None = None
    override: PasteOverride = attrs.field(factory=PasteOverride)


@attrs.frozen
class ResolvedPaste:
    """1 pad に対する解決済みの確定塗布設定.

    Attributes:
        enabled: 塗布対象か
        dispense_mode: 塗布方式 auto / dot / line / area
        paste_height: 塗布面の Z 高さ [mm]、または auto
        ul_per_mm2: パッド面積あたりのペースト量 [μL/mm²]
        prime_extra_delay: プライム後の追加遅延 [sec]
        bead_width_factor: ビード幅係数
        overlap: ジグザグ行間オーバーラップ [0,1)
        boundary_margin: 外周マージン [mm]
    """

    enabled: bool
    dispense_mode: DispenseMode
    paste_height: PasteHeight
    ul_per_mm2: float
    prime_extra_delay: float
    bead_width_factor: float
    overlap: float
    boundary_margin: float


@attrs.frozen
class PasteSettingsModel:
    """基板1枚分の塗布設定モデル.

    Attributes:
        base: machine.toml 由来のデフォルト（全項目確定）
        base_enabled: machine.toml 由来の有効/無効既定
        initial_purge_pad_id: 初回パージに使う pad id（None = fill sequence 先頭）
        levels: L0–L4 の疎マップ（``HierKey`` -> 設定）
    """

    base: PasteOverride
    base_enabled: bool = True
    initial_purge_pad_id: str | None = None
    levels: dict[HierKey, LevelSetting] = attrs.field(factory=dict)

    def with_level_patch(
        self,
        key: HierKey,
        *,
        values: dict[str, PasteSettingValue] | None = None,
        clear: Sequence[str] = (),
        enabled: bool | None = None,
        enabled_sent: bool = False,
    ) -> "PasteSettingsModel":
        """1 ノードの override を upsert/clear した新しいモデルを返す（不変）.

        - ``values`` の項目で override を上書きする
        - ``clear`` の項目を継承（``None``）に戻す
        - ``enabled_sent`` が False のときは既存の ``enabled`` を保持する
        - 結果が空（``enabled`` が None かつ override 全項目 None）なら
          ``levels`` から ``key`` を除く
        """
        levels = dict(self.levels)
        current = levels.get(key, LevelSetting())
        override_dict: dict[str, PasteSettingValue | None] = {
            field: getattr(current.override, field) for field in PASTE_OVERRIDE_FIELDS
        }
        if values:
            override_dict.update(values)
        for field in clear:
            override_dict[field] = None
        new_override = PasteOverride(**cast(dict[str, Any], override_dict))
        new_enabled = enabled if enabled_sent else current.enabled
        if new_enabled is None and not _has_override_values(new_override):
            levels.pop(key, None)
        else:
            levels[key] = LevelSetting(enabled=new_enabled, override=new_override)
        return attrs.evolve(self, levels=levels)

    def with_pads_enabled(
        self, l4_keys: Sequence[HierKey], *, enabled: bool
    ) -> "PasteSettingsModel":
        """指定 L4 ノード群の ``enabled`` を一括設定した新モデルを返す."""
        model = self
        for key in l4_keys:
            model = model.with_level_patch(key, enabled=enabled, enabled_sent=True)
        return model

    def with_initial_purge_pad_id(self, pad_id: str | None) -> "PasteSettingsModel":
        """初回パージ pad id を差し替えた新モデルを返す."""
        return attrs.evolve(self, initial_purge_pad_id=pad_id)


def base_override_from_config(config: PasteDispenser) -> PasteOverride:
    """``PasteDispenser`` 設定から L0 デフォルトの override を作る.

    全項目が ``config`` 由来の確定値（非 None）になる。

    Args:
        config: マシンのペーストディスペンサー設定

    Returns:
        全項目確定の :class:`PasteOverride`
    """
    return PasteOverride(
        dispense_mode=config.dispense_mode,
        paste_height=config.paste_height,
        ul_per_mm2=config.ul_per_mm2,
        prime_extra_delay=config.prime_extra_delay,
        bead_width_factor=config.bead_width_factor,
        overlap=config.overlap,
        boundary_margin=config.boundary_margin,
    )


def _check_numeric(field: str, value: object) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field}は数値である必要があります: {value!r}")


def _check_paste_height(value: object) -> None:
    if value == "auto":
        return
    _check_numeric("paste_height", value)
    if isinstance(value, (int, float)) and value <= 0:
        raise ValueError(f"paste_heightは正の値である必要があります: {value}")


def _has_override_values(override: PasteOverride) -> bool:
    """Override に非 None 項目が1つでもあるか."""
    return any(getattr(override, field) is not None for field in PASTE_OVERRIDE_FIELDS)


def validate_field_names(fields: Sequence[str]) -> str | None:
    """``PASTE_OVERRIDE_FIELDS`` 外の項目があればエラー文、無ければ ``None``."""
    unknown = [field for field in fields if field not in PASTE_OVERRIDE_FIELDS]
    if unknown:
        return f"未知の設定項目です: {', '.join(unknown)}"
    return None


def validate_override_values(values: dict[str, PasteSettingValue]) -> str | None:
    """Override patch の値を検証し、不正なら日本語エラー文、正常なら ``None``.

    ``PasteOverride`` 生成前に HTTP リクエスト値を検査するための None 返却
    バリデーション。``PasteOverride.__attrs_post_init__`` と同一規則。
    """
    if (message := validate_field_names(list(values))) is not None:
        return message
    for field, value in values.items():
        if field == "dispense_mode":
            if not isinstance(value, str) or value not in DISPENSE_MODES:
                return f"未知の塗布方式です: {value!r}"
        elif field == "paste_height":
            if value == "auto":
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"paste_heightはautoまたは数値で指定してください: {value!r}"
            if value <= 0:
                return f"paste_heightは正の値で指定してください: {value}"
        elif field in NUMERIC_PASTE_OVERRIDE_FIELDS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"{field}は数値で指定してください: {value!r}"
    return None


def _apply_override(
    values: dict[str, PasteSettingValue], override: PasteOverride
) -> None:
    """非 None 項目のみ ``values`` に上書きする（in-place）."""
    for field in PASTE_OVERRIDE_FIELDS:
        value = getattr(override, field)
        if value is not None:
            values[field] = value


def resolve_pad_settings(
    hierarchy: PadHierarchy, model: PasteSettingsModel
) -> dict[PadRef, ResolvedPaste]:
    """各 pad の確定塗布設定を解決する.

    各 pad で ``hierarchy.node_keys_for_pad(pad)`` の L0→L4 を順に辿り、
    ``model.levels`` にある設定の非 None 項目で上書きする。``enabled`` も
    最具体レベルの明示値が勝つ。起点は ``model.base`` / ``model.base_enabled``。

    Args:
        hierarchy: pad 階層
        model: 塗布設定モデル

    Returns:
        ``(designator, pad_ref)`` -> :class:`ResolvedPaste`。通常 pad では
        ``pad_ref == pad_number``。同一 ``pad_number`` の分割 pad では
        ``#1`` / ``#2`` suffix 付き参照になる。
    """
    result: dict[PadRef, ResolvedPaste] = {}
    for pad in hierarchy.iter_pads():
        values: dict[str, PasteSettingValue] = {
            field: getattr(model.base, field) for field in PASTE_OVERRIDE_FIELDS
        }
        enabled = model.base_enabled
        for key in hierarchy.node_keys_for_pad(pad):
            setting = model.levels.get(key)
            if setting is None:
                continue
            _apply_override(values, setting.override)
            if setting.enabled is not None:
                enabled = setting.enabled
        result[hierarchy.pad_ref_for_pad(pad)] = _resolved_from_values(enabled, values)
    return result


def _resolved_from_values(
    enabled: bool, values: dict[str, PasteSettingValue]
) -> ResolvedPaste:
    """解決済み values dict を :class:`ResolvedPaste` に変換する."""
    return ResolvedPaste(
        enabled=enabled,
        dispense_mode=_dispense_mode_value(values["dispense_mode"]),
        paste_height=_paste_height_value(values["paste_height"]),
        ul_per_mm2=_float_value("ul_per_mm2", values["ul_per_mm2"]),
        prime_extra_delay=_float_value(
            "prime_extra_delay", values["prime_extra_delay"]
        ),
        bead_width_factor=_float_value(
            "bead_width_factor", values["bead_width_factor"]
        ),
        overlap=_float_value("overlap", values["overlap"]),
        boundary_margin=_float_value("boundary_margin", values["boundary_margin"]),
    )


def is_pad_enabled(
    pad: Pad, hierarchy: PadHierarchy, resolved: Mapping[PadRef, ResolvedPaste]
) -> bool:
    """Pad が塗布対象か判定する.

    階層から除外された pad（対応 Component 無し = ``resolved`` に不在）は
    後方互換で有効扱い、それ以外は解決済み ``enabled`` に従う。
    """
    try:
        pad_ref = hierarchy.pad_ref_for_pad(pad)
    except KeyError:
        return True
    r = resolved.get(pad_ref)
    return r is None or r.enabled


def select_enabled_pads(
    pads: Iterable[Pad], hierarchy: PadHierarchy, model: PasteSettingsModel
) -> list[Pad]:
    """塗布対象（enabled）の pad だけを元の順序で返す.

    :func:`resolve_pad_settings` を 1 回だけ呼び、:func:`is_pad_enabled` の
    規則で filter する。プレビュー（webui router）と実行（webui job）が
    同一の絞り込みを共有するための単一ソース。
    """
    resolved = resolve_pad_settings(hierarchy, model)
    return [pad for pad in pads if is_pad_enabled(pad, hierarchy, resolved)]


def resolve_node_settings(
    hierarchy: PadHierarchy, model: PasteSettingsModel
) -> dict[HierKey, ResolvedPaste]:
    """各階層ノード（L0–L4）の確定塗布設定を解決する.

    pad 単位の :func:`resolve_pad_settings` と同じ規則を、ツリーの各ノードに
    ついて「ルートから自ノードまで」のパスで適用する（最具体が勝つ）。UI の
    階層表が各ノード行に解決済み値を表示するための算出。

    Args:
        hierarchy: pad 階層
        model: 塗布設定モデル

    Returns:
        ``HierKey`` -> :class:`ResolvedPaste`
    """
    result: dict[HierKey, ResolvedPaste] = {}
    base_values: dict[str, PasteSettingValue] = {
        field: getattr(model.base, field) for field in PASTE_OVERRIDE_FIELDS
    }
    _resolve_node(hierarchy.root, base_values, model.base_enabled, model, result)
    return result


def _resolve_node(
    node: PadHierarchyNode,
    parent_values: dict[str, PasteSettingValue],
    parent_enabled: bool,
    model: PasteSettingsModel,
    result: dict[HierKey, ResolvedPaste],
) -> None:
    """ルートから累積した値で ``node`` を解決し、子へ再帰する（in-place）."""
    values = dict(parent_values)
    enabled = parent_enabled
    setting = model.levels.get(node.key)
    if setting is not None:
        _apply_override(values, setting.override)
        if setting.enabled is not None:
            enabled = setting.enabled
    result[node.key] = _resolved_from_values(enabled, values)
    for child in node.children:
        _resolve_node(child, values, enabled, model, result)


def _dispense_mode_value(value: PasteSettingValue) -> DispenseMode:
    if isinstance(value, str) and value in DISPENSE_MODES:
        return value
    raise ValueError(f"未知の塗布方式です: {value!r}")


def _paste_height_value(value: PasteSettingValue) -> PasteHeight:
    if value == "auto":
        return "auto"
    return _float_value("paste_height", value)


def _float_value(field: str, value: PasteSettingValue) -> float:
    _check_numeric(field, value)
    assert isinstance(value, (int, float))
    return float(value)


def find_orphans(model: PasteSettingsModel, hierarchy: PadHierarchy) -> list[HierKey]:
    """現階層に存在しない設定キー（孤児）を返す.

    Args:
        model: 塗布設定モデル
        hierarchy: 現在の pad 階層

    Returns:
        ``model.levels`` のうち ``hierarchy`` に対応ノードが無いキー
    """
    existing = hierarchy.all_keys()
    return [key for key in model.levels if key not in existing]


def _override_to_dict(override: PasteOverride) -> dict[str, PasteSettingValue]:
    """非 None 項目のみの dict に変換する."""
    return {
        field: value
        for field in PASTE_OVERRIDE_FIELDS
        if (value := getattr(override, field)) is not None
    }


def _override_from_dict(data: dict) -> PasteOverride:
    """欠落項目を None として :class:`PasteOverride` を復元する."""
    return PasteOverride(**{field: data.get(field) for field in PASTE_OVERRIDE_FIELDS})


def settings_to_dict(model: PasteSettingsModel) -> dict:
    """設定モデルを JSON 可能な dict に変換する.

    ``levels`` は tuple キーが JSON 化できないため list に展開する。
    ``override`` は非 None 項目のみを保持する。``version`` / ``source_pcb``
    は付けない（webui の責務）。

    Args:
        model: 塗布設定モデル

    Returns:
        ``{"base": {...}, "base_enabled": bool, "levels": [...]}``
    """
    return {
        "base": _override_to_dict(model.base),
        "base_enabled": model.base_enabled,
        "initial_purge_pad_id": model.initial_purge_pad_id,
        "levels": [
            {
                "key": list(key),
                "enabled": setting.enabled,
                "override": _override_to_dict(setting.override),
            }
            for key, setting in model.levels.items()
        ],
    }


def settings_from_dict(data: dict) -> PasteSettingsModel:
    """``settings_to_dict`` の逆変換で設定モデルを復元する.

    Args:
        data: ``settings_to_dict`` 形式の dict

    Returns:
        復元した :class:`PasteSettingsModel`
    """
    levels: dict[HierKey, LevelSetting] = {}
    for entry in _as_sequence(data.get("levels")):
        key = tuple(entry["key"])
        levels[key] = LevelSetting(
            enabled=entry.get("enabled"),
            override=_override_from_dict(entry.get("override", {})),
        )
    return PasteSettingsModel(
        base=_override_from_dict(data.get("base", {})),
        base_enabled=data.get("base_enabled", True),
        initial_purge_pad_id=data.get("initial_purge_pad_id"),
        levels=levels,
    )


def _as_sequence(value: object) -> Sequence:
    """Levels が欠落/None の場合に空列を返すヘルパ."""
    if isinstance(value, Sequence):
        return value
    return []
