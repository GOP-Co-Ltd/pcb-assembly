"""Pad-config API の契約モデルとビュー構築ヘルパ.

選択中の基板について、pad ジオメトリ・階層ツリー・解決済み塗布設定・
疎な override をレスポンス形式へ変換する。pcbnew 依存は ``PcbFile`` が
関数内 import するため、本モジュール自体は KiCAD 未導入環境でも import
できる。エンドポイント定義は :mod:`web.api.routers.pasting` に置く。

node_id 規約（フロントと共有する契約）:

- ``HierKey`` tuple ⇔ node_id 文字列は ``":".join(key)`` / ``tuple(s.split(":"))``
- L0 = ``"L0"``、L1 = ``"L1:{package}"``、L2 = ``"L2:{designator}"``、
  L3 = ``"L3:{designator}:{shape_label}"``、L4 = ``"L4:{designator}:{pad_ref}"``
- pad id = ``f"{designator}.{pad_ref}"``。通常 ``pad_ref == pad_number``。
  同一 pad number の分割 pad は ``#1`` / ``#2`` suffix で区別する。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator

import attrs
from fastapi import HTTPException
from pydantic import BaseModel

from pcbasm.config import PasteDispenser
from pcbasm.pasting.fill_path import build_pad_fill_plan
from pcbasm.pasting.initial_purge import (
    InitialPurgePurpose,
    resolve_initial_purge_for,
)
from pcbasm.pasting.params import PASTE_PARAM_FIELDS, PasteParamValue
from pcbasm.pasting.route import plan_paste_route, routed_enabled_pads
from pcbasm.pasting.settings import (
    PasteSettingsModel,
    ResolvedSetting,
    descendant_override_summary,
    own_override_summary,
    resolve_node_settings,
    resolve_pad_settings,
    select_enabled_pads,
)
from pcbasm.pcb import (
    Layer,
    Pad,
    PadHierarchy,
    PadHierarchyNode,
    PcbFile,
)
from web.api.board_settings import BoardSettingsStore
from web.api.settings import Settings
from web.api.state import AppState


# --------------------------------------------------------------------------- #
# pydantic 契約モデル
# --------------------------------------------------------------------------- #
class ResolvedSettings(BaseModel):
    """解決済みの確定塗布設定（enabled + override 項目）."""

    enabled: bool
    dispense_mode: str
    line_direction: str
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
    values: dict[str, PasteParamValue] = {}  # override された項目のみ（疎）


class DescendantSummary(BaseModel):
    """子孫ノードの override 集計（UI の継承マーカー表示用）."""

    enabled_count: int = 0  # enabled を明示した子孫ノード数
    field_counts: dict[str, int] = {}  # field ごとの子孫 override 数
    fields: list[str] = []  # 子孫に override がある field
    node_count: int = 0  # override を持つ子孫ノード数
    count: int = 0  # 子孫 override の総数


class OwnSummary(BaseModel):
    """ノード自身の明示 override の集計（UI の行バッジ表示用）."""

    enabled: bool  # enabled を明示しているか
    fields: list[str]  # override された field（UI 表示順）
    count: int  # fields + enabled の総数


class HierNodeInfo(BaseModel):
    """階層ツリーの 1 ノード（構造 + 解決値・own override・子孫集計）."""

    id: str
    level: int
    label: str
    resolved: ResolvedSettings  # このノードに解決される確定値（enabled 含む）
    own_override: NodeOverrideInfo  # このノードの明示 override（疎）
    own_summary: OwnSummary  # own_override の表示用集計
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
    selection_label: str
    error: str | None


class InitialPurgeResponse(BaseModel):
    """PATCH 初回パージ設定のレスポンス."""

    initial_purge: InitialPurgeInfo


class ChoiceInfo(BaseModel):
    """選択式パラメータの 1 選択肢."""

    value: str
    label: str


class ParamFieldInfo(BaseModel):
    """塗布パラメータ 1 項目の UI メタデータ（列順 = :data:`PASTE_PARAM_FIELDS`）.

    JS はこれを唯一の出典として列見出し・入力種別・選択肢を描く（クライアント側に フィールド定義を複製しない）。
    """

    name: str
    label: str
    kind: str  # number / choice / height
    unit: str | None
    choices: list[ChoiceInfo]


class PadConfigResponse(BaseModel):
    """GET /api/pasting/pad-config のレスポンス."""

    pcb_file: str
    outline: list[list[float]]
    width: float
    height: float
    defaults: ResolvedSettings  # machine.toml 由来の基板デフォルト
    initial_purge: InitialPurgeInfo
    tree: HierNodeInfo  # L0 ルートの階層ツリー（構造のみ）
    pads: list[PadInfo]
    overrides: dict[str, NodeOverrideInfo]  # node_id -> 明示 override（疎、L0 含む）
    fields: list[ParamFieldInfo]  # 塗布パラメータの UI メタデータ（列順）


class PasteRouteRequest(BaseModel):
    """POST /api/pasting/pad-config/route のリクエスト."""

    layer: Layer = Layer.TOP


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

    layer: Layer = Layer.TOP


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
    values: dict[str, PasteParamValue] = {}  # upsert する override
    clear: list[str] = []  # 継承に戻す override 項目
    expected_pcb: str | None = None  # 編集開始時の PCB（不一致なら 409）


class PadEnablePatch(BaseModel):
    """PATCH /api/pasting/pad-config/pads のリクエスト."""

    ids: list[str]
    enabled: bool
    expected_pcb: str | None = None  # 編集開始時の PCB（不一致なら 409）


class InitialPurgePatch(BaseModel):
    """PATCH /api/pasting/pad-config/initial-purge のリクエスト."""

    initial_purge_ul: float | None = None
    pad_id: str | None = None
    expected_pcb: str | None = None  # 編集開始時の PCB（不一致なら 409）


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
def node_id(key: tuple[str, ...]) -> str:
    return ":".join(key)


def key_from_node_id(node: str) -> tuple[str, ...]:
    return tuple(node.split(":"))


# --------------------------------------------------------------------------- #
# 変換ヘルパ
# --------------------------------------------------------------------------- #
def resolved_settings(resolved: ResolvedSetting) -> ResolvedSettings:
    return ResolvedSettings(enabled=resolved.enabled, **resolved.params.to_dict())


def tree(
    node: PadHierarchyNode,
    model: PasteSettingsModel,
    resolved: dict[tuple[str, ...], ResolvedSetting],
) -> HierNodeInfo:
    setting = model.level(node.key)
    own = NodeOverrideInfo(
        enabled=setting.enabled if setting is not None else None,
        values=setting.patch.to_dict() if setting is not None else {},
    )
    return HierNodeInfo(
        id=node_id(node.key),
        level=node.level,
        label=node.label,
        resolved=resolved_settings(resolved[node.key]),
        own_override=own,
        own_summary=OwnSummary(**attrs.asdict(own_override_summary(setting))),
        descendant_summary=DescendantSummary(
            **attrs.asdict(descendant_override_summary(node, model))
        ),
        children=[tree(child, model, resolved) for child in node.children],
    )


def overrides(model: PasteSettingsModel) -> dict[str, NodeOverrideInfo]:
    """model.levels の明示 override を node_id キーへ変換する."""
    return {
        node_id(setting.key): NodeOverrideInfo(
            enabled=setting.enabled, values=setting.patch.to_dict()
        )
        for setting in model.levels
    }


def resolved_default(model: PasteSettingsModel) -> ResolvedSettings:
    """machine.toml 由来の基板既定値を返す.

    ``model.base`` は machine.toml 由来で全 override 項目が確定（非 None）。
    """
    return ResolvedSettings(enabled=True, **model.base.to_dict())


def layer_pads(loaded: Loaded, layer: Layer) -> Iterator[Pad]:
    """指定 layer の全 pad（有効/無効問わず）を返す."""
    return (pad for pad in loaded.hierarchy.iter_pads() if pad.layer is layer)


def param_fields() -> list[ParamFieldInfo]:
    """:data:`PASTE_PARAM_FIELDS` をレスポンス形式へ写す（列順を保つ）."""
    return [
        ParamFieldInfo(
            name=field.name,
            label=field.label,
            kind=field.kind,
            unit=field.unit,
            choices=[
                ChoiceInfo(value=choice.value, label=choice.label)
                for choice in field.choices
            ],
        )
        for field in PASTE_PARAM_FIELDS
    ]


def build_initial_purge(
    loaded: Loaded, purpose: InitialPurgePurpose = "paste_solder"
) -> InitialPurgeInfo:
    """ロード済みコンテキストから初回パージ設定の解決結果を返す."""
    routed = routed_enabled_pads(
        layer_pads(loaded, Layer.TOP), loaded.hierarchy, loaded.model
    )
    resolution = resolve_initial_purge_for(
        purpose,
        amount_ul=loaded.base_config.initial_purge_ul,
        pad_id=loaded.model.initial_purge_pad_id,
        hierarchy=loaded.hierarchy,
        routed_pads=routed,
    )
    resolved, default_pad_id, error = (
        resolution.resolved,
        resolution.default_pad_id,
        resolution.error,
    )
    if error is not None and purpose == "paste_solder":
        raise HTTPException(status_code=400, detail=error)
    selection_label = (
        loaded.model.initial_purge_pad_id
        if loaded.model.initial_purge_pad_id is not None
        else (
            f"自動 ({default_pad_id})"
            if default_pad_id is not None
            else "自動 (設定が必要)"
        )
    )
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
        selection_label=selection_label,
        error=error,
    )


def pad_info(
    pad: Pad,
    pad_id: str,
    package: str,
    node_ids: list[str],
    resolved: ResolvedSetting,
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
        resolved=resolved_settings(resolved),
    )


# --------------------------------------------------------------------------- #
# 共通: PCB / 階層 / モデルのロード
# --------------------------------------------------------------------------- #
@attrs.frozen
class Loaded:
    source_pcb: str
    base_config: PasteDispenser
    pcb: PcbFile
    hierarchy: PadHierarchy
    board_signature: str
    model: PasteSettingsModel


def load_board(
    state: AppState, settings: Settings, board_store: BoardSettingsStore
) -> Loaded:
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
    base_config = state.machine().paste_dispenser
    hierarchy = PadHierarchy.build(pcb.components, pcb.pads)
    signature = hierarchy.signature()
    model = board_store.load_or_init(source_pcb, base_config, board_signature=signature)
    return Loaded(
        source_pcb=source_pcb,
        base_config=base_config,
        pcb=pcb,
        hierarchy=hierarchy,
        board_signature=signature,
        model=model,
    )


def build_pad_config(
    loaded: Loaded, purpose: InitialPurgePurpose = "paste_solder"
) -> PadConfigResponse:
    """ロード済みコンテキストから GET 形式のレスポンスを構築する."""
    pcb = loaded.pcb
    hierarchy = loaded.hierarchy
    model = loaded.model

    resolved = resolve_pad_settings(hierarchy, model)
    node_resolved = resolve_node_settings(hierarchy, model)
    package_by_designator = {c.designator: c.package for c in pcb.components}

    pads = [
        pad_info(
            pad,
            hierarchy.pad_id_for_pad(pad),
            package_by_designator.get(pad.designator, ""),
            [node_id(key) for key in hierarchy.node_keys_for_pad(pad)],
            resolved[hierarchy.pad_ref_for_pad(pad)],
        )
        for pad in hierarchy.iter_pads()
    ]

    outline = pcb.outline
    return PadConfigResponse(
        pcb_file=loaded.source_pcb,
        outline=[[x, y] for x, y in outline.polygon.exterior.coords],
        width=outline.width,
        height=outline.height,
        defaults=resolved_default(model),
        initial_purge=build_initial_purge(loaded, purpose),
        tree=tree(hierarchy.root, model, node_resolved),
        pads=pads,
        overrides=overrides(model),
        fields=param_fields(),
    )


def build_route(loaded: Loaded, layer: Layer) -> PasteRouteResponse:
    """ロード済みコンテキストから有効 pad の順路レスポンスを構築する."""
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
                layer_pads(loaded, layer), loaded.hierarchy, loaded.model
            )
        )
    ]
    return PasteRouteResponse(layer=layer.value, pads=route)


def build_fill_path(loaded: Loaded, layer: Layer) -> PasteFillPathResponse:
    """ロード済みコンテキストから有効 pad の塗布パスを構築する."""
    nozzle_diameter = loaded.base_config.nozzle_diameter
    resolved = resolve_pad_settings(loaded.hierarchy, loaded.model)
    component_positions = {
        component.designator: component.position for component in loaded.pcb.components
    }
    pads: list[PasteFillPathPad] = []
    for pad in loaded.hierarchy.iter_pads():
        setting = resolved[loaded.hierarchy.pad_ref_for_pad(pad)]
        if pad.layer is not layer or not setting.enabled:
            continue
        plan = build_pad_fill_plan(
            pad.polygon,
            config=loaded.base_config,
            params=setting.params,
            line_reference=component_positions.get(pad.designator),
        )
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
        layer=layer.value,
        nozzle_diameter=nozzle_diameter,
        pads=pads,
    )


def affected_pads(node: str, loaded: Loaded) -> list[AffectedPad]:
    """指定ノード配下の全 pad を再解決して返す."""
    key = key_from_node_id(node)
    return _affected(loaded, lambda pad: key in loaded.hierarchy.node_keys_for_pad(pad))


def affected_pads_for_ids(ids: Iterable[str], loaded: Loaded) -> list[AffectedPad]:
    """指定 pad id 群を再解決して返す."""
    id_set = set(ids)
    return _affected(loaded, lambda pad: loaded.hierarchy.pad_id_for_pad(pad) in id_set)


def _affected(loaded: Loaded, include: Callable[[Pad], bool]) -> list[AffectedPad]:
    resolved = resolve_pad_settings(loaded.hierarchy, loaded.model)
    return [
        AffectedPad(
            id=loaded.hierarchy.pad_id_for_pad(pad),
            enabled=paste.enabled,
            resolved=resolved_settings(paste),
        )
        for pad in loaded.hierarchy.iter_pads()
        if include(pad)
        for paste in (resolved[loaded.hierarchy.pad_ref_for_pad(pad)],)
    ]
