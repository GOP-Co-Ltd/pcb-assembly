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

from collections.abc import Iterator

import attrs
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from pcbasm.config import PasteDispenser
from pcbasm.pasting import (
    PASTE_OVERRIDE_FIELDS,
    PasteOverride,
    PasteSettingsModel,
    PasteSettingValue,
    ResolvedPaste,
    base_override_from_config,
    build_pad_fill_plan_for,
    estimate_mass_flow,
    plan_paste_route,
    resolve_initial_purge,
    resolve_node_settings,
    resolve_pad_settings,
    routed_enabled_pads,
    select_enabled_pads,
    validate_field_names,
    validate_initial_purge,
    validate_override_values,
)
from pcbasm.pcb import (
    Layer,
    Pad,
    PadHierarchy,
    PadHierarchyNode,
    PcbFile,
    build_pad_hierarchy,
)
from webui.app import BoardStoreDep, SettingsDep, StateDep
from webui.board_settings import BoardSettingsStore
from webui.settings import Settings
from webui.state import AppState

router = APIRouter(prefix="/api")


# --------------------------------------------------------------------------- #
# pydantic 契約モデル
# --------------------------------------------------------------------------- #
class ResolvedSettings(BaseModel):
    """解決済みの確定塗布設定（enabled + override 項目）."""

    enabled: bool
    dispense_mode: str
    paste_height: float | str
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


class NodeOverrideInfo(BaseModel):
    """1 ノードに明示された override（疎）."""

    enabled: bool | None = None  # 明示 enabled（無指定 = null）
    values: dict[str, PasteSettingValue] = {}  # override された項目のみ（疎）


class DescendantSummary(BaseModel):
    """子孫ノードの override 集計（UI の継承マーカー表示用）."""

    enabled_count: int = 0  # enabled を明示した子孫ノード数
    field_counts: dict[str, int] = {}  # field ごとの子孫 override 数
    fields: list[str] = []  # 子孫に override がある field
    node_count: int = 0  # override を持つ子孫ノード数
    count: int = 0  # 子孫 override の総数


class HierNodeInfo(BaseModel):
    """階層ツリーの 1 ノード（構造 + 解決値・own override・子孫集計）."""

    id: str
    level: int
    label: str
    resolved: ResolvedSettings  # このノードに解決される確定値（enabled 含む）
    own_override: NodeOverrideInfo  # このノードの明示 override（疎）
    descendant_summary: DescendantSummary  # 子孫ノードの override 集計
    children: list[HierNodeInfo]


class ResolvedInitialPurgeInfo(BaseModel):
    """初回パージの実行対象として解決された pad 情報."""

    pad_id: str
    amount: float
    point: list[float]
    source: str


class InitialPurgeInfo(BaseModel):
    """初回パージ設定とサーバ側解決結果."""

    initial_purge_ul: float
    pad_id: str | None
    default_pad_id: str | None
    resolved: ResolvedInitialPurgeInfo | None


class InitialPurgeResponse(BaseModel):
    """PATCH 初回パージ設定のレスポンス."""

    initial_purge: InitialPurgeInfo


class PadConfigResponse(BaseModel):
    """GET /api/pasting/pad-config のレスポンス."""

    pcb_file: str
    machine: str
    outline: list[list[float]]
    width: float
    height: float
    defaults: ResolvedSettings  # machine.toml 由来の基板デフォルト
    initial_purge: InitialPurgeInfo
    tree: HierNodeInfo  # L0 ルートの階層ツリー（構造のみ）
    pads: list[PadInfo]
    overrides: dict[str, NodeOverrideInfo]  # node_id -> 明示 override（疎、L0 含む）


class PasteRouteRequest(BaseModel):
    """POST /api/pasting/pad-config/route のリクエスト."""

    layer: str = "Top"


class PasteRoutePad(BaseModel):
    """順路上の 1 pad."""

    id: str
    order: int
    group_label: str
    area: float
    center: list[float]


class PasteRouteResponse(BaseModel):
    """有効 pad の塗布順路."""

    layer: str
    pads: list[PasteRoutePad]


class PasteFillPathRequest(BaseModel):
    """POST /api/pasting/pad-config/fill-path のリクエスト."""

    layer: str = "Top"


class PasteFillPathPad(BaseModel):
    """1 pad の塗布パス."""

    id: str
    dispense_mode: str
    path_count: int
    point_count: int
    paths: list[list[list[float]]]


class PasteFillPathResponse(BaseModel):
    """有効 pad の塗布パス."""

    layer: str
    nozzle_diameter: float
    pads: list[PasteFillPathPad]


class NodePatch(BaseModel):
    """PATCH /api/pasting/pad-config/node のリクエスト."""

    node: str
    enabled: bool | None = None  # "enabled" in model_fields_set で送信有無を判定
    values: dict[str, PasteSettingValue] = {}  # upsert する override
    clear: list[str] = []  # 継承に戻す override 項目


class PadEnablePatch(BaseModel):
    """PATCH /api/pasting/pad-config/pads のリクエスト."""

    ids: list[str]
    enabled: bool


class InitialPurgePatch(BaseModel):
    """PATCH /api/pasting/pad-config/initial-purge のリクエスト."""

    initial_purge_ul: float | None = None
    pad_id: str | None = None


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
    # ResolvedPaste と ResolvedSettings は同名フィールド（enabled + override 項目）。
    return ResolvedSettings(**attrs.asdict(resolved))


def _tree(
    node: PadHierarchyNode,
    model: PasteSettingsModel,
    resolved: dict[tuple[str, ...], ResolvedPaste],
) -> HierNodeInfo:
    setting = model.levels.get(node.key)
    own = NodeOverrideInfo(
        enabled=setting.enabled if setting is not None else None,
        values=_override_values(setting.override) if setting is not None else {},
    )
    return HierNodeInfo(
        id=_node_id(node.key),
        level=node.level,
        label=node.label,
        resolved=_resolved_settings(resolved[node.key]),
        own_override=own,
        descendant_summary=_descendant_summary(node, model),
        children=[_tree(child, model, resolved) for child in node.children],
    )


def _descendant_summary(
    node: PadHierarchyNode, model: PasteSettingsModel
) -> DescendantSummary:
    """``node`` の子孫（自身は含めない）の override を集計する."""
    field_counts = {field: 0 for field in PASTE_OVERRIDE_FIELDS}
    enabled_count = 0
    node_count = 0
    total = 0
    for descendant in _iter_descendants(node):
        setting = model.levels.get(descendant.key)
        if setting is None:
            continue
        own_count = 0
        if setting.enabled is not None:
            enabled_count += 1
            own_count += 1
        for field in PASTE_OVERRIDE_FIELDS:
            if getattr(setting.override, field) is not None:
                field_counts[field] += 1
                own_count += 1
        if own_count > 0:
            total += own_count
            node_count += 1
    return DescendantSummary(
        enabled_count=enabled_count,
        field_counts=field_counts,
        fields=[field for field in PASTE_OVERRIDE_FIELDS if field_counts[field] > 0],
        node_count=node_count,
        count=total,
    )


def _iter_descendants(node: PadHierarchyNode) -> Iterator[PadHierarchyNode]:
    for child in node.children:
        yield child
        yield from _iter_descendants(child)


def _override_values(override: PasteOverride) -> dict[str, PasteSettingValue]:
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

    ``model.base`` は machine.toml 由来で全 override 項目が確定（非 None）。
    """
    return ResolvedSettings(
        enabled=model.base_enabled,
        **{field: getattr(model.base, field) for field in PASTE_OVERRIDE_FIELDS},
    )


def _layer_pads(loaded: _Loaded, layer: str) -> Iterator[Pad]:
    """指定 layer の全 pad（有効/無効問わず）を返す."""
    return (pad for pad in loaded.hierarchy.iter_pads() if pad.layer.value == layer)


def _build_initial_purge(loaded: _Loaded) -> InitialPurgeInfo:
    """ロード済みコンテキストから初回パージ設定の解決結果を返す."""
    routed = routed_enabled_pads(
        _layer_pads(loaded, Layer.TOP.value), loaded.hierarchy, loaded.model
    )
    default_pad_id = loaded.hierarchy.pad_id_for_pad(routed[0]) if routed else None
    resolved, error = resolve_initial_purge(
        amount_ul=loaded.base_config.initial_purge_ul,
        pad_id=loaded.model.initial_purge_pad_id,
        hierarchy=loaded.hierarchy,
        routed_pads=routed,
        layer=Layer.TOP,
    )
    if error is not None:
        raise HTTPException(status_code=400, detail=error)
    return InitialPurgeInfo(
        initial_purge_ul=loaded.base_config.initial_purge_ul,
        pad_id=loaded.model.initial_purge_pad_id,
        default_pad_id=default_pad_id,
        resolved=(
            ResolvedInitialPurgeInfo(
                pad_id=resolved.pad_id,
                amount=resolved.amount_ul,
                point=[resolved.pad.center.x, resolved.pad.center.y],
                source=resolved.source,
            )
            if resolved is not None
            else None
        ),
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
    signature = hierarchy.signature()
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
    node_resolved = resolve_node_settings(hierarchy, model)
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
        initial_purge=_build_initial_purge(loaded),
        tree=_tree(hierarchy.root, model, node_resolved),
        pads=pads,
        overrides=_overrides(model),
    )


def _build_route(loaded: _Loaded, layer: str) -> PasteRouteResponse:
    """ロード済みコンテキストから有効 pad の順路レスポンスを構築する."""
    _check_layer(layer)

    route = [
        PasteRoutePad(
            id=loaded.hierarchy.pad_id_for_pad(stop.pad),
            order=stop.order,
            group_label=stop.group_label,
            area=stop.area,
            center=[stop.pad.center.x, stop.pad.center.y],
        )
        for stop in plan_paste_route(
            select_enabled_pads(
                _layer_pads(loaded, layer), loaded.hierarchy, loaded.model
            )
        )
    ]
    return PasteRouteResponse(layer=layer, pads=route)


def _build_fill_path(loaded: _Loaded, layer: str) -> PasteFillPathResponse:
    """ロード済みコンテキストから有効 pad の塗布パスを構築する."""
    _check_layer(layer)

    nozzle_diameter = loaded.base_config.nozzle_diameter
    resolved = resolve_pad_settings(loaded.hierarchy, loaded.model)
    pads: list[PasteFillPathPad] = []
    for pad in loaded.hierarchy.iter_pads():
        paste = resolved[loaded.hierarchy.pad_ref_for_pad(pad)]
        if pad.layer.value != layer or not paste.enabled:
            continue
        try:
            plan = build_pad_fill_plan_for(
                pad.polygon,
                nozzle_diameter=nozzle_diameter,
                auto_line_aspect_ratio=loaded.base_config.auto_line_aspect_ratio,
                auto_area_short_side_factor=(
                    loaded.base_config.auto_area_short_side_factor
                ),
                paste=paste,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"塗布パス設定が不正です: {exc}"
            ) from exc
        points = [[[point.x, point.y] for point in path] for path in plan.paths]
        pads.append(
            PasteFillPathPad(
                id=loaded.hierarchy.pad_id_for_pad(pad),
                dispense_mode=plan.dispense_mode,
                path_count=len(points),
                point_count=sum(len(path) for path in points),
                paths=points,
            )
        )
    return PasteFillPathResponse(
        layer=layer,
        nozzle_diameter=nozzle_diameter,
        pads=pads,
    )


def _check_layer(layer: str) -> None:
    valid_layers = {member.value for member in Layer}
    if layer not in valid_layers:
        raise HTTPException(status_code=400, detail=f"未知のレイヤです: {layer}")


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
# エンドポイント
# --------------------------------------------------------------------------- #
@router.get("/pasting/pad-config")
def get_pad_config(
    state: StateDep, settings: SettingsDep, board_store: BoardStoreDep
) -> PadConfigResponse:
    """選択中基板の pad ジオメトリ・階層・解決済み設定・疎 override を返す."""
    return _build_pad_config(_load(state, settings, board_store))


@router.post("/pasting/pad-config/route")
def calculate_pad_route(
    body: PasteRouteRequest,
    state: StateDep,
    settings: SettingsDep,
    board_store: BoardStoreDep,
) -> PasteRouteResponse:
    """選択中基板の有効 pad だけを対象に塗布順路を返す."""
    return _build_route(_load(state, settings, board_store), body.layer)


@router.post("/pasting/pad-config/fill-path")
def calculate_pad_fill_path(
    body: PasteFillPathRequest,
    state: StateDep,
    settings: SettingsDep,
    board_store: BoardStoreDep,
) -> PasteFillPathResponse:
    """選択中基板の有効 pad だけを対象に塗布パスを返す."""
    return _build_fill_path(_load(state, settings, board_store), body.layer)


@router.patch("/pasting/pad-config/node")
def patch_pad_config_node(
    body: NodePatch,
    state: StateDep,
    settings: SettingsDep,
    board_store: BoardStoreDep,
) -> PatchResponse:
    """ノードの enabled/values upsert・clear を適用し、影響 pad を返す."""
    loaded = _load(state, settings, board_store)
    if (message := validate_override_values(body.values)) is not None:
        raise HTTPException(status_code=400, detail=message)
    if (message := validate_field_names(body.clear)) is not None:
        raise HTTPException(status_code=400, detail=message)
    key = _key_from_node_id(body.node)
    if key not in loaded.hierarchy.all_keys():
        raise HTTPException(status_code=400, detail=f"未知のノードです: {body.node}")
    new_model = loaded.model.with_level_patch(
        key,
        values=body.values,
        clear=body.clear,
        enabled=body.enabled,
        enabled_sent="enabled" in body.model_fields_set,
    )
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
    l4_keys, unknown = loaded.hierarchy.l4_keys_for_pad_ids(body.ids)
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"未知の pad です: {', '.join(unknown)}"
        )
    model = loaded.model.with_pads_enabled(l4_keys, enabled=body.enabled)
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


@router.patch("/pasting/pad-config/initial-purge")
def patch_initial_purge(
    body: InitialPurgePatch,
    state: StateDep,
    settings: SettingsDep,
    board_store: BoardStoreDep,
) -> InitialPurgeResponse:
    """初回パージ量と pad 指定を即時保存し、解決済み設定を返す."""
    loaded = _load(state, settings, board_store)
    amount_sent = "initial_purge_ul" in body.model_fields_set
    pad_sent = "pad_id" in body.model_fields_set
    if amount_sent and body.initial_purge_ul is None:
        raise HTTPException(
            status_code=400, detail="initial_purge_ulは数値で指定してください"
        )

    next_amount = (
        body.initial_purge_ul if amount_sent else loaded.base_config.initial_purge_ul
    )
    assert next_amount is not None
    next_pad_id = (
        _normalize_initial_purge_pad_id(body.pad_id)
        if pad_sent
        else loaded.model.initial_purge_pad_id
    )
    routed = routed_enabled_pads(
        _layer_pads(loaded, Layer.TOP.value), loaded.hierarchy, loaded.model
    )
    error = validate_initial_purge(
        amount_ul=next_amount,
        pad_id=next_pad_id,
        hierarchy=loaded.hierarchy,
        routed_pads=routed,
        layer=Layer.TOP,
    )
    if error is not None:
        raise HTTPException(status_code=400, detail=error)

    if amount_sent:
        with state.machine_lock("pasting-initial-purge"):
            state.write_machine_settings(
                {"paste_dispenser.initial_purge_ul": next_amount}
            )
    if pad_sent:
        model = loaded.model.with_initial_purge_pad_id(next_pad_id)
        board_store.save(
            loaded.machine,
            loaded.source_pcb,
            model,
            board_signature=loaded.board_signature,
        )
    return InitialPurgeResponse(
        initial_purge=_build_initial_purge(_load(state, settings, board_store))
    )


def _normalize_initial_purge_pad_id(pad_id: str | None) -> str | None:
    """API 入力の空文字を未指定へ正規化する."""
    return None if pad_id in (None, "") else pad_id


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
    filename = f"pcbasm-paste-overrides-{board_store.board_id(loaded.source_pcb)}.json"
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


# --------------------------------------------------------------------------- #
# ペーストローディング 質量キャリブレーション（算出のみ。適用は settings API）
# --------------------------------------------------------------------------- #
class LoadingCalibrationResult(BaseModel):
    """質量キャリブレーションの算出結果（入力不足の値は null）."""

    volume_ul: float | None
    rotations_per_ul: float | None
    max_dispense_rate: float | None
    dispense_accel: float | None


@router.get("/pasting/loading/calibration")
def get_loading_calibration(
    state: StateDep,
    mass_mg: float = 0.0,
    rotations: float = 0.0,
    rate: float = 0.0,
    accel: float = 0.0,
) -> LoadingCalibrationResult:
    """計測質量・回転数・速度・加速度から塗布キャリブレーション値を算出する.

    密度はサーバ側のマシン設定 ``solder_paste_density`` を真実とする。
    非正入力は該当値を ``None`` で返す（エラーにしない）。算出は
    :func:`pcbasm.pasting.estimate_mass_flow` へ委譲する（丸め済み）。
    """
    density = state.machine().paste_dispenser.solder_paste_density
    estimate = estimate_mass_flow(
        mass_mg=mass_mg,
        rotations=rotations,
        rate=rate,
        accel=accel,
        density_mg_per_ul=density,
    )
    return LoadingCalibrationResult(**attrs.asdict(estimate))
