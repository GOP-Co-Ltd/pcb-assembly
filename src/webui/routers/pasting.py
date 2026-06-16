"""塗布タブの pad-config API（基板ごとの pad 有効/無効 + 階層 override）.

選択中の基板について、pad ジオメトリ・階層ツリー・解決済み塗布設定・
疎な override を 1 発で返し（GET）、ノード/pad 単位の編集を即時保存する
（PATCH）。pcbnew 依存は ``PcbFile`` が関数内 import するため、本ルーター
自体は KiCAD 未導入環境でも import できる。

node_id 規約（フロントと共有する契約）:

- ``HierKey`` tuple ⇔ node_id 文字列は ``":".join(key)`` / ``tuple(s.split(":"))``
- L0 = ``"L0"``、L1 = ``"L1:{package}"``、L2 = ``"L2:{designator}"``、
  L3 = ``"L3:{designator}:{shape_label}"``、L4 = ``"L4:{designator}:{pad_ref}"``
- pad id = ``f"{designator}.{pad_ref}"``。通常 ``pad_ref == pad_number``。
  同一 pad number の分割 pad は ``#1`` / ``#2`` suffix で区別する。
"""

from __future__ import annotations

import attrs
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from pcbasm.config import PasteDispenser
from pcbasm.pasting import (
    PASTE_OVERRIDE_FIELDS,
    LevelSetting,
    PasteOverride,
    PasteSettingsModel,
    ResolvedPaste,
    base_override_from_config,
    resolve_pad_settings,
)
from pcbasm.pcb import (
    Pad,
    PadHierarchy,
    PadHierarchyNode,
    PcbFile,
    build_pad_hierarchy,
)
from webui.app import BoardStoreDep, SettingsDep, StateDep
from webui.board_settings import BoardSettingsStore, board_signature
from webui.settings import Settings
from webui.state import AppState

router = APIRouter(prefix="/api")


# --------------------------------------------------------------------------- #
# pydantic 契約モデル
# --------------------------------------------------------------------------- #
class ResolvedSettings(BaseModel):
    """解決済みの確定塗布設定（enabled + 7 項目）."""

    enabled: bool
    fill_speed: float
    paste_height: float
    ul_per_mm2: float
    prime_extra_delay: float
    bead_width_factor: float
    overlap: float
    boundary_margin: float


class PadInfo(BaseModel):
    """1 pad のジオメトリと解決済み設定."""

    id: str
    node_ids: list[str]
    designator: str
    pad_number: str
    package: str
    net_name: str
    layer: str  # "Top" / "Bottom"
    polygon: list[list[float]]  # exterior 座標 [[x, y], ...]
    enabled: bool
    resolved: ResolvedSettings


class HierNodeInfo(BaseModel):
    """階層ツリーの 1 ノード（構造のみ。pad は含めない）."""

    id: str
    level: int
    label: str
    children: list[HierNodeInfo]


class NodeOverrideInfo(BaseModel):
    """1 ノードに明示された override（疎）."""

    enabled: bool | None = None  # 明示 enabled（無指定 = null）
    values: dict[str, float] = {}  # override された項目のみ（疎）


class PadConfigResponse(BaseModel):
    """GET /api/pasting/pad-config のレスポンス."""

    pcb_file: str
    machine: str
    outline: list[list[float]]
    width: float
    height: float
    defaults: ResolvedSettings  # machine.toml 由来の基板デフォルト
    tree: HierNodeInfo  # L0 ルートの階層ツリー（構造のみ）
    pads: list[PadInfo]
    overrides: dict[str, NodeOverrideInfo]  # node_id -> 明示 override（疎、L0 含む）


class NodePatch(BaseModel):
    """PATCH /api/pasting/pad-config/node のリクエスト."""

    node: str
    enabled: bool | None = None  # "enabled" in model_fields_set で送信有無を判定
    values: dict[str, float] = {}  # upsert する override
    clear: list[str] = []  # 継承に戻す override 項目


class PadEnablePatch(BaseModel):
    """PATCH /api/pasting/pad-config/pads のリクエスト."""

    ids: list[str]
    enabled: bool


class PadConfigImport(BaseModel):
    """POST /api/pasting/pad-config/import のリクエスト."""

    document: dict


class AffectedPad(BaseModel):
    """編集の影響を受けた pad の最新状態."""

    id: str
    enabled: bool
    resolved: ResolvedSettings


class PatchResponse(BaseModel):
    """PATCH 系のレスポンス（影響 pad の再解決）."""

    affected_pads: list[AffectedPad]


# --------------------------------------------------------------------------- #
# node_id <-> HierKey 変換
# --------------------------------------------------------------------------- #
def _node_id(key: tuple[str, ...]) -> str:
    return ":".join(key)


def _key_from_node_id(node: str) -> tuple[str, ...]:
    return tuple(node.split(":"))


# --------------------------------------------------------------------------- #
# 変換ヘルパ
# --------------------------------------------------------------------------- #
def _resolved_settings(resolved: ResolvedPaste) -> ResolvedSettings:
    # ResolvedPaste と ResolvedSettings は同名フィールド（enabled + 7 項目）。
    return ResolvedSettings(**attrs.asdict(resolved))


def _tree(node: PadHierarchyNode) -> HierNodeInfo:
    return HierNodeInfo(
        id=_node_id(node.key),
        level=node.level,
        label=node.label,
        children=[_tree(child) for child in node.children],
    )


def _override_values(override: PasteOverride) -> dict[str, float]:
    return {
        field: value
        for field in PASTE_OVERRIDE_FIELDS
        if (value := getattr(override, field)) is not None
    }


def _overrides(model: PasteSettingsModel) -> dict[str, NodeOverrideInfo]:
    """model.levels の明示 override を node_id キーへ変換する."""
    result: dict[str, NodeOverrideInfo] = {}
    for key, setting in model.levels.items():
        result[_node_id(key)] = NodeOverrideInfo(
            enabled=setting.enabled, values=_override_values(setting.override)
        )
    return result


def _resolved_default(model: PasteSettingsModel) -> ResolvedSettings:
    """machine.toml 由来の基板既定値を返す.

    ``model.base`` は machine.toml 由来で全 7 項目が確定（非 None）。
    """
    return ResolvedSettings(
        enabled=model.base_enabled,
        **{field: getattr(model.base, field) for field in PASTE_OVERRIDE_FIELDS},
    )


def _pad_info(
    pad: Pad,
    pad_id: str,
    package: str,
    node_ids: list[str],
    resolved: ResolvedPaste,
) -> PadInfo:
    return PadInfo(
        id=pad_id,
        node_ids=node_ids,
        designator=pad.designator,
        pad_number=pad.pad_number,
        package=package,
        net_name=pad.net_name,
        layer=pad.layer.value,
        polygon=[[x, y] for x, y in pad.polygon.exterior.coords],
        enabled=resolved.enabled,
        resolved=_resolved_settings(resolved),
    )


# --------------------------------------------------------------------------- #
# 共通: PCB / 階層 / モデルのロード
# --------------------------------------------------------------------------- #
@attrs.frozen
class _Loaded:
    source_pcb: str
    machine: str
    base_config: PasteDispenser
    pcb: PcbFile
    hierarchy: PadHierarchy
    board_signature: str
    model: PasteSettingsModel


def _load(
    state: AppState, settings: Settings, board_store: BoardSettingsStore
) -> _Loaded:
    """選択中基板の PcbFile / 階層 / 保存済みモデルをまとめてロードする.

    Raises:
        HTTPException: PCB 未選択（409）の場合
    """
    pcb_rel = state.selected_pcb
    if pcb_rel is None:
        raise HTTPException(status_code=409, detail="PCB が選択されていません")
    source_pcb = pcb_rel.as_posix()
    abs_path = settings.pcb_browse_root / pcb_rel
    pcb = PcbFile(abs_path)
    machine = state.selected_machine
    base_config = state.machine().paste_dispenser
    hierarchy = build_pad_hierarchy(pcb.components, pcb.pads)
    signature = board_signature(hierarchy)
    model = board_store.load_or_init(
        machine, source_pcb, base_config, board_signature=signature
    )
    return _Loaded(
        source_pcb=source_pcb,
        machine=machine,
        base_config=base_config,
        pcb=pcb,
        hierarchy=hierarchy,
        board_signature=signature,
        model=model,
    )


def _build_pad_config(loaded: _Loaded) -> PadConfigResponse:
    """ロード済みコンテキストから GET 形式のレスポンスを構築する."""
    pcb = loaded.pcb
    hierarchy = loaded.hierarchy
    model = loaded.model

    resolved = resolve_pad_settings(hierarchy, model)
    package_by_designator = {c.designator: c.package for c in pcb.components}

    pads = [
        _pad_info(
            pad,
            hierarchy.pad_id_for_pad(pad),
            package_by_designator.get(pad.designator, ""),
            [_node_id(key) for key in hierarchy.node_keys_for_pad(pad)],
            resolved[hierarchy.pad_ref_for_pad(pad)],
        )
        for pad in hierarchy.iter_pads()
    ]

    outline = pcb.outline
    return PadConfigResponse(
        pcb_file=loaded.source_pcb,
        machine=loaded.machine,
        outline=[[x, y] for x, y in outline.polygon.exterior.coords],
        width=outline.width,
        height=outline.height,
        defaults=_resolved_default(model),
        tree=_tree(hierarchy.root),
        pads=pads,
        overrides=_overrides(model),
    )


def _affected_pads(node: str, loaded: _Loaded) -> list[AffectedPad]:
    """指定ノード配下の全 pad を再解決して返す."""
    key = _key_from_node_id(node)
    resolved = resolve_pad_settings(loaded.hierarchy, loaded.model)
    affected: list[AffectedPad] = []
    for pad in loaded.hierarchy.iter_pads():
        keys = loaded.hierarchy.node_keys_for_pad(pad)
        if key not in keys:
            continue
        paste = resolved[loaded.hierarchy.pad_ref_for_pad(pad)]
        affected.append(
            AffectedPad(
                id=loaded.hierarchy.pad_id_for_pad(pad),
                enabled=paste.enabled,
                resolved=_resolved_settings(paste),
            )
        )
    return affected


# --------------------------------------------------------------------------- #
# ノード更新ロジック（webui 側で attrs.evolve。pcbasm settings.py は変更しない）
# --------------------------------------------------------------------------- #
def _check_known_fields(fields: list[str]) -> None:
    unknown = [f for f in fields if f not in PASTE_OVERRIDE_FIELDS]
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"未知の設定項目です: {', '.join(unknown)}"
        )


def _apply_node_patch(
    model: PasteSettingsModel, patch: NodePatch, hierarchy: PadHierarchy
) -> PasteSettingsModel:
    """Node patch を適用した新しいモデルを返す.

    Raises:
        HTTPException: 未知の項目（values/clear）・未知ノード（400）の場合
    """
    _check_known_fields(list(patch.values))
    _check_known_fields(patch.clear)

    return _apply_level_patch(
        model,
        patch,
        hierarchy,
        enabled_sent="enabled" in patch.model_fields_set,
    )


def _apply_level_patch(
    model: PasteSettingsModel,
    patch: NodePatch,
    hierarchy: PadHierarchy,
    enabled_sent: bool,
) -> PasteSettingsModel:
    key = _key_from_node_id(patch.node)
    if key not in hierarchy.all_keys():
        raise HTTPException(status_code=400, detail=f"未知のノードです: {patch.node}")

    levels = dict(model.levels)
    current = levels.get(key, LevelSetting())

    override_dict = {
        field: getattr(current.override, field) for field in PASTE_OVERRIDE_FIELDS
    }
    override_dict.update(patch.values)
    for field in patch.clear:
        override_dict[field] = None
    new_override = PasteOverride(**override_dict)

    enabled = patch.enabled if enabled_sent else current.enabled

    if enabled is None and not _override_values(new_override):
        levels.pop(key, None)
    else:
        levels[key] = LevelSetting(enabled=enabled, override=new_override)
    return attrs.evolve(model, levels=levels)


# --------------------------------------------------------------------------- #
# エンドポイント
# --------------------------------------------------------------------------- #
@router.get("/pasting/pad-config")
def get_pad_config(
    state: StateDep, settings: SettingsDep, board_store: BoardStoreDep
) -> PadConfigResponse:
    """選択中基板の pad ジオメトリ・階層・解決済み設定・疎 override を返す."""
    return _build_pad_config(_load(state, settings, board_store))


@router.patch("/pasting/pad-config/node")
def patch_pad_config_node(
    body: NodePatch,
    state: StateDep,
    settings: SettingsDep,
    board_store: BoardStoreDep,
) -> PatchResponse:
    """ノードの enabled/values upsert・clear を適用し、影響 pad を返す."""
    loaded = _load(state, settings, board_store)
    new_model = _apply_node_patch(loaded.model, body, loaded.hierarchy)
    board_store.save(
        loaded.machine,
        loaded.source_pcb,
        new_model,
        board_signature=loaded.board_signature,
    )
    updated = attrs.evolve(loaded, model=new_model)
    return PatchResponse(affected_pads=_affected_pads(body.node, updated))


@router.patch("/pasting/pad-config/pads")
def patch_pad_config_pads(
    body: PadEnablePatch,
    state: StateDep,
    settings: SettingsDep,
    board_store: BoardStoreDep,
) -> PatchResponse:
    """Pad id 配列を L4 ノードの enabled 設定として一括適用する."""
    loaded = _load(state, settings, board_store)
    model = loaded.model
    for pad_id in body.ids:
        try:
            l4_key = loaded.hierarchy.l4_key_for_pad_id(pad_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=400, detail=f"未知の pad です: {pad_id}"
            ) from exc
        patch = NodePatch(node=_node_id(l4_key), enabled=body.enabled)
        model = _apply_node_patch(model, patch, loaded.hierarchy)
    board_store.save(
        loaded.machine,
        loaded.source_pcb,
        model,
        board_signature=loaded.board_signature,
    )

    resolved = resolve_pad_settings(loaded.hierarchy, model)
    id_set = set(body.ids)
    affected = [
        AffectedPad(
            id=loaded.hierarchy.pad_id_for_pad(pad),
            enabled=paste.enabled,
            resolved=_resolved_settings(paste),
        )
        for pad in loaded.hierarchy.iter_pads()
        if loaded.hierarchy.pad_id_for_pad(pad) in id_set
        for paste in (resolved[loaded.hierarchy.pad_ref_for_pad(pad)],)
    ]
    return PatchResponse(affected_pads=affected)


@router.post("/pasting/pad-config/reset")
def reset_pad_config(
    state: StateDep, settings: SettingsDep, board_store: BoardStoreDep
) -> PadConfigResponse:
    """全 override を破棄し、machine.toml 由来の新規モデルを保存して返す."""
    loaded = _load(state, settings, board_store)
    fresh = PasteSettingsModel(
        base=base_override_from_config(loaded.base_config),
        base_enabled=True,
        levels={},
    )
    board_store.save(
        loaded.machine,
        loaded.source_pcb,
        fresh,
        board_signature=loaded.board_signature,
    )
    return _build_pad_config(attrs.evolve(loaded, model=fresh))


@router.get("/pasting/pad-config/export")
def export_pad_config(
    state: StateDep, settings: SettingsDep, board_store: BoardStoreDep
) -> JSONResponse:
    """現在の基板 override 設定をダウンロード用 JSON として返す."""
    loaded = _load(state, settings, board_store)
    doc = board_store.export_doc(
        loaded.machine,
        loaded.source_pcb,
        loaded.model,
        board_signature=loaded.board_signature,
    )
    filename = (
        f"pcbasm-paste-overrides-" f"{board_store.board_id(loaded.source_pcb)}.json"
    )
    return JSONResponse(
        content=doc,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/pasting/pad-config/import")
def import_pad_config(
    body: PadConfigImport,
    state: StateDep,
    settings: SettingsDep,
    board_store: BoardStoreDep,
) -> PadConfigResponse:
    """アップロードされた基板 override 設定を検証して保存し、最新設定を返す."""
    loaded = _load(state, settings, board_store)
    try:
        model = board_store.model_from_doc(
            body.document,
            loaded.base_config,
            board_signature=loaded.board_signature,
            expected_machine=loaded.machine,
            expected_source_pcb=loaded.source_pcb,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    pruned = board_store.prune(
        loaded.machine,
        loaded.source_pcb,
        model,
        loaded.hierarchy,
        board_signature=loaded.board_signature,
    )
    return _build_pad_config(attrs.evolve(loaded, model=pruned))
