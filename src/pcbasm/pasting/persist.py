"""基板ごとの塗布設定 JSON の encode / decode.

真実の源は ``machine.toml`` の ``[paste_dispenser]`` 値で、基板 JSON には
明示 override（L0 を含む ``levels``）と基板上の座標設定だけを保持する。
ファイル I/O・ロック・保存先の決定は web 層（``BoardSettingsStore``）の責務。

保存形式（schema v1）::

    {
        "version": 1,
        "source_pcb": "<pcb_browse_root からの相対 posix パス>",
        "board_signature": "<基板構成ハッシュ>",   # 任意
        "settings": {
            "levels": [{"key": ["L2", "U1"], "enabled": null, "override": {...}}, ...],
            "initial_purge_point": [12.5, 8.0],      # 任意（未設定 = 順路先頭）
            "flow_calibration_point": [20.0, 8.0]    # 任意（未設定 = 補正しない）
        }
    }

版の扱い: キー／型が変わるときだけ ``BOARD_SETTINGS_SCHEMA_VERSION`` を上げ、
:func:`decode_board_settings` が版で分岐して旧版を純関数で新版 dict に写す。
現行の唯一の移行は、初期 v1 が ``settings`` 直下に持っていた
``base`` / ``base_enabled`` を L0 の明示設定へ畳み込むもの。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import attrs

from pcbasm.geometry import Point2d
from pcbasm.pasting.params import PASTE_PARAM_NAMES, PasteParams, PasteParamsPatch
from pcbasm.pasting.settings import LevelSetting, PasteSettingsModel
from pcbasm.pcb.grouping import HierKey
from pcbasm.utils import is_finite_number

BOARD_SETTINGS_SCHEMA_VERSION = 1

_L0_KEY: HierKey = ("L0",)


@attrs.frozen
class DecodedBoardSettings:
    """Decode 結果（モデルと保存時のメタ情報）."""

    model: PasteSettingsModel
    source_pcb: str | None
    board_signature: str | None


def encode_board_settings(
    model: PasteSettingsModel,
    *,
    source_pcb: str,
    board_signature: str | None = None,
) -> dict[str, Any]:
    """モデルを保存 JSON ドキュメント（dict）に変換する."""
    settings: dict[str, Any] = {
        "levels": [
            {
                "key": list(setting.key),
                "enabled": setting.enabled,
                "override": setting.patch.to_dict(),
            }
            for setting in model.levels
        ]
    }
    if model.initial_purge_point is not None:
        point = model.initial_purge_point
        settings["initial_purge_point"] = [point.x, point.y]
    if model.flow_calibration_point is not None:
        point = model.flow_calibration_point
        settings["flow_calibration_point"] = [point.x, point.y]
    doc: dict[str, Any] = {
        "version": BOARD_SETTINGS_SCHEMA_VERSION,
        "source_pcb": source_pcb,
        "settings": settings,
    }
    if board_signature is not None:
        doc["board_signature"] = board_signature
    return doc


def decode_board_settings(
    doc: Mapping[str, Any], *, base: PasteParams
) -> tuple[DecodedBoardSettings | None, str | None]:
    """保存 JSON ドキュメントを現在の machine.toml デフォルト ``base`` に重ねて復元する.

    Returns:
        ``(decoded, None)`` または ``(None, エラー文)``
    """
    version = doc.get("version")
    if version != BOARD_SETTINGS_SCHEMA_VERSION:
        return None, (
            f"未知の board_settings schema version です: {version} "
            f"(対応 v{BOARD_SETTINGS_SCHEMA_VERSION})"
        )
    settings = doc.get("settings")
    if not isinstance(settings, Mapping):
        return None, "settings がありません"

    levels = [
        _level_from_entry(entry) for entry in _as_sequence(settings.get("levels"))
    ]
    keys = {setting.key for setting in levels}
    if _L0_KEY not in keys:
        legacy_l0 = _legacy_l0_setting(settings, base)
        if legacy_l0 is not None:
            levels.append(legacy_l0)

    source_pcb = doc.get("source_pcb")
    board_signature = doc.get("board_signature")
    model = PasteSettingsModel(
        base=base,
        initial_purge_point=_point(settings.get("initial_purge_point")),
        flow_calibration_point=_point(settings.get("flow_calibration_point")),
        levels=tuple(levels),
    )
    return (
        DecodedBoardSettings(
            model=model,
            source_pcb=source_pcb if isinstance(source_pcb, str) else None,
            board_signature=(
                board_signature if isinstance(board_signature, str) else None
            ),
        ),
        None,
    )


def _level_from_entry(entry: Mapping[str, Any]) -> LevelSetting:
    return LevelSetting(
        key=tuple(entry["key"]),
        enabled=entry.get("enabled"),
        patch=PasteParamsPatch.from_dict(entry.get("override") or {}),
    )


def _legacy_l0_setting(
    settings: Mapping[str, Any], base: PasteParams
) -> LevelSetting | None:
    """初期 v1 の ``base`` / ``base_enabled`` を L0 の明示設定へ必要分だけ移行する.

    保存時の base のうち現在の machine.toml 値と異なる項目だけを override にする。
    """
    saved_base = settings.get("base")
    values = (
        {
            name: saved
            for name in PASTE_PARAM_NAMES
            if (saved := saved_base.get(name)) is not None
            and saved != getattr(base, name)
        }
        if isinstance(saved_base, Mapping)
        else {}
    )
    enabled = False if settings.get("base_enabled") is False else None
    if enabled is None and not values:
        return None
    return LevelSetting(
        _L0_KEY, enabled=enabled, patch=PasteParamsPatch.from_dict(values)
    )


def _as_sequence(value: object) -> Sequence[Any]:
    return value if isinstance(value, Sequence) and not isinstance(value, str) else []


def _point(value: object) -> Point2d | None:
    """保存値 ``[x, y]`` を Point2d へ戻す（形が違えば ``None``）.

    壊れた保存内容でページを開けなくしないため、pad id と同じく黙って捨てる。
    """
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return None
    if len(value) != 2:
        return None
    x, y = value
    if not is_finite_number(x) or not is_finite_number(y):
        return None
    return Point2d(float(x), float(y))
