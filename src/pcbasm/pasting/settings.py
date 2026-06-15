"""塗布設定の override モデルと階層解決.

塗布実行（``PasteApplicator``）と UI プレビューが同一の解決規則を
共有するため、装置非依存の純ロジックとしてここに集約する。

設定モデルは override 思想:

- :class:`PasteOverride` の各項目は ``None`` = 継承（上位レベルの値を使う）
- :class:`PasteSettingsModel.base` が全項目確定の L0 デフォルト
- :class:`PasteSettingsModel.levels` が L1–L4 の疎マップ
- 解決時は各 pad で L0→L4 を辿り、非 None 項目で上書き（**最具体が勝つ**）
"""

from collections.abc import Sequence

import attrs

from pcbasm.config import PasteDispenser
from pcbasm.pcb.grouping import HierKey, PadHierarchy

# override 可能な7項目のフィールド名（解決・JSON 変換の正準順）
PASTE_OVERRIDE_FIELDS: tuple[str, ...] = (
    "fill_speed",
    "paste_height",
    "ul_per_mm2",
    "prime_extra_delay",
    "bead_width_factor",
    "overlap",
    "boundary_margin",
)

# 有効/無効の3値（True / False / None=継承）
EnableState = bool | None


@attrs.frozen
class PasteOverride:
    """塗布パラメータの差分上書き（``None`` = 継承）.

    7項目すべてが ``float | None``。``None`` の項目は上位レベルの値を
    継承する（JSON では欠落で表現）。
    """

    fill_speed: float | None = None
    paste_height: float | None = None
    ul_per_mm2: float | None = None
    prime_extra_delay: float | None = None
    bead_width_factor: float | None = None
    overlap: float | None = None
    boundary_margin: float | None = None


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
        fill_speed: 塗布移動速度 [mm/sec]
        paste_height: 塗布面の Z 高さ [mm]
        ul_per_mm2: パッド面積あたりのペースト量 [μL/mm²]
        prime_extra_delay: プライム後の追加遅延 [sec]
        bead_width_factor: ビード幅係数
        overlap: ジグザグ行間オーバーラップ [0,1)
        boundary_margin: 外周マージン [mm]
    """

    enabled: bool
    fill_speed: float
    paste_height: float
    ul_per_mm2: float
    prime_extra_delay: float
    bead_width_factor: float
    overlap: float
    boundary_margin: float


@attrs.frozen
class PasteSettingsModel:
    """基板1枚分の塗布設定モデル.

    Attributes:
        base: L0 デフォルト（全項目確定）
        base_enabled: L0 の有効/無効
        levels: L1–L4 の疎マップ（``HierKey`` -> 設定）
    """

    base: PasteOverride
    base_enabled: bool = True
    levels: dict[HierKey, LevelSetting] = attrs.field(factory=dict)


def base_override_from_config(config: PasteDispenser) -> PasteOverride:
    """``PasteDispenser`` 設定から L0 デフォルトの override を作る.

    7項目すべてが ``config`` 由来の確定値（非 None）になる。

    Args:
        config: マシンのペーストディスペンサー設定

    Returns:
        全項目確定の :class:`PasteOverride`
    """
    return PasteOverride(
        fill_speed=config.fill_speed,
        paste_height=config.paste_height,
        ul_per_mm2=config.ul_per_mm2,
        prime_extra_delay=config.prime_extra_delay,
        bead_width_factor=config.bead_width_factor,
        overlap=config.overlap,
        boundary_margin=config.boundary_margin,
    )


def _apply_override(values: dict[str, float], override: PasteOverride) -> None:
    """非 None 項目のみ ``values`` に上書きする（in-place）."""
    for field in PASTE_OVERRIDE_FIELDS:
        value = getattr(override, field)
        if value is not None:
            values[field] = value


def resolve_pad_settings(
    hierarchy: PadHierarchy, model: PasteSettingsModel
) -> dict[tuple[str, str], ResolvedPaste]:
    """各 pad の確定塗布設定を解決する.

    各 pad で ``hierarchy.node_keys_for_pad(pad)`` の L0→L4 を順に辿り、
    ``model.levels`` にある設定の非 None 項目で上書きする。``enabled`` も
    最具体レベルの明示値が勝つ。起点は ``model.base`` / ``model.base_enabled``。

    Args:
        hierarchy: pad 階層
        model: 塗布設定モデル

    Returns:
        ``(designator, pad_number)`` -> :class:`ResolvedPaste`
    """
    result: dict[tuple[str, str], ResolvedPaste] = {}
    for pad in hierarchy.iter_pads():
        values: dict[str, float] = {
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
        result[(pad.designator, pad.pad_number)] = ResolvedPaste(
            enabled=enabled, **values
        )
    return result


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


def _override_to_dict(override: PasteOverride) -> dict[str, float]:
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
        levels=levels,
    )


def _as_sequence(value: object) -> Sequence:
    """Levels が欠落/None の場合に空列を返すヘルパ."""
    if isinstance(value, Sequence):
        return value
    return []
