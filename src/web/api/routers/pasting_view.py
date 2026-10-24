"""Pad-config API の契約モデルとビュー構築ヘルパ.

選択中の基板について、pad ジオメトリ・階層ツリー・解決済み塗布設定・
疎な override をレスポンス形式へ変換する。エンドポイント定義は :mod:`web.api.routers.pasting` に置く。

node_id 規約（フロントと共有する契約）:

- ``HierKey`` tuple ⇔ node_id 文字列は ``":".join(key)`` / ``tuple(s.split(":"))``
- L0 = ``"L0"``、L1 = ``"L1:{package}"``、L2 = ``"L2:{designator}"``、
  L3 = ``"L3:{designator}:{shape_label}"``、L4 = ``"L4:{designator}:{pad_ref}"``
- pad id = ``f"{designator}.{pad_ref}"``。通常 ``pad_ref == pad_number``。
  同一 pad number の分割 pad は ``#1`` / ``#2`` suffix で区別する。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

import attrs
from fastapi import HTTPException
from pydantic import BaseModel

from pcbasm.board import Layer, Pad
from pcbasm.geometry import Point2d
from pcbasm.pasting.hierarchy import PadHierarchyNode
from pcbasm.pasting.overrides import (
    BoardOverrides,
    BoardSession,
    PasteSettingsModel,
    ResolvedSetting,
    descendant_override_summary,
    own_override_summary,
    resolve_node_settings,
    resolve_pad_settings,
)
from pcbasm.pasting.pad_editor import (
    COPPER_DISPLAY_TOLERANCE_MM,
    AppliedModeSummary,
    applied_mode_summaries,
    display_rings,
    flow_calibration_view,
    initial_purge_view,
)
from pcbasm.pasting.params import PASTE_PARAM_FIELDS, PasteParamValue
from pcbasm.pasting.plan import PastePlan, plan_paste
from web.api.env import Env
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
    micro_line_z_start: float | str
    micro_line_z_end: float | str


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


class AppliedValueInfo(BaseModel):
    """解決値の選択から読み取れない、実際に使われる値の表示（UI の補足表示用）."""

    label: str  # 表示文字列（例: "→ 点 12 / 線 6"）
    title: str  # ツールチップ
    counts: dict[str, int]  # 値ごとの有効 pad 数（選択肢の並び順）


class HierNodeInfo(BaseModel):
    """階層ツリーの 1 ノード（構造 + 解決値・own override・子孫集計）."""

    id: str
    level: int
    label: str
    resolved: ResolvedSettings  # このノードに解決される確定値（enabled 含む）
    height_fields: list[str]  # 塗布高さの列に出す項目名（解決済みの方式で決まる）
    own_override: NodeOverrideInfo  # このノードの明示 override（疎）
    own_summary: OwnSummary  # own_override の表示用集計
    descendant_summary: DescendantSummary  # 子孫ノードの override 集計
    # field 名 -> 実際に使われる値（選択から読み取れるノードには載せない）
    applied: dict[str, AppliedValueInfo]
    children: list[HierNodeInfo]


class ResolvedInitialPurgeInfo(BaseModel):
    """初回パージの実行対象として解決された塗布点情報."""

    label: str
    amount: float
    point: list[float]
    source: str


class InitialPurgeInfo(BaseModel):
    """初回パージ設定とサーバ側解決結果."""

    initial_purge_ul: float
    point: list[float] | None  # 明示指定（``None`` = 自動）
    default_point: list[float] | None  # 自動時に使う面積最大 pad の中心
    resolved: ResolvedInitialPurgeInfo | None
    selection_label: str
    error: str | None


class InitialPurgeResponse(BaseModel):
    """PATCH 初回パージ設定のレスポンス."""

    initial_purge: InitialPurgeInfo


class FlowCalibrationInfo(BaseModel):
    """運転時流量キャリブレーションの設定とサーバ側解決結果.

    machine.toml 側（校正ファイル・塗布量・crop 寸法）と基板側（測定位置）の両方が
    そろってはじめて動く。
    どちらが欠けているかは ``selection_label`` に出す。

    ``points`` は保存されている測定位置そのもので、補正が成立しない状態でも返す。
    設定した点が図から消えると位置を直せないため。
    """

    enabled: bool  # machine.toml 側が有効か
    points: list[list[float]]  # 基板ごとの測定位置（塗る順。空 = 未設定）
    overlapping: list[bool]  # 点ごとに、他の点と撮影範囲が重なっているか
    planned: bool  # 実際に補正を走らせる状態か
    amount_ul: float
    crop_size_mm: float  # 測定位置ごとの撮影範囲の一辺（図の点線の四角）
    selection_label: str
    error: str | None


class FlowCalibrationResponse(BaseModel):
    """PATCH 流量キャリブレーション位置のレスポンス."""

    flow_calibration: FlowCalibrationInfo


class ChoiceInfo(BaseModel):
    """選択式パラメータの 1 選択肢."""

    value: str
    label: str


class ParamFieldInfo(BaseModel):
    """塗布パラメータ 1 項目の UI メタデータ（列順 = :data:`PASTE_PARAM_FIELDS`）.

    JS はこれを唯一の出典として列見出し・入力種別・選択肢を描く（クライアント側にフィールド定義を複製しない）。
    """

    name: str
    label: str
    kind: str  # number / choice / height
    unit: str | None
    choices: list[ChoiceInfo]
    column: str | None  # pad 表でこの項目を描く列の項目名（独立した列なら None）


class PadConfigResponse(BaseModel):
    """GET /api/pasting/pad-config のレスポンス."""

    pcb_file: str
    outline: list[list[float]]
    width: float
    height: float
    defaults: ResolvedSettings  # machine.toml 由来の基板デフォルト
    initial_purge: InitialPurgeInfo
    flow_calibration: FlowCalibrationInfo
    tree: HierNodeInfo  # L0 ルートの階層ツリー（構造のみ）
    pads: list[PadInfo]
    overrides: dict[str, NodeOverrideInfo]  # node_id -> 明示 override（疎、L0 含む）
    fields: list[ParamFieldInfo]  # 塗布パラメータの UI メタデータ（列順）


class CopperIslandInfo(BaseModel):
    """表示用の銅箔島（exterior が先、以降が穴）."""

    layer: str  # "Top" / "Bottom"
    rings: list[list[list[float]]]


class PadConfigCopperResponse(BaseModel):
    """GET /api/pasting/pad-config/copper のレスポンス."""

    pcb_file: str
    tolerance_mm: float
    islands: list[CopperIslandInfo]


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
    expected_pcb: str  # 編集開始時の PCB（不一致なら 409）


class PadEnablePatch(BaseModel):
    """PATCH /api/pasting/pad-config/pads のリクエスト."""

    ids: list[str]
    enabled: bool
    expected_pcb: str  # 編集開始時の PCB（不一致なら 409）


class InitialPurgePatch(BaseModel):
    """PATCH /api/pasting/pad-config/initial-purge のリクエスト."""

    initial_purge_ul: float | None = None
    point: list[float] | None = None  # board 座標 [x, y]（``None`` で自動へ戻す）
    expected_pcb: str  # 編集開始時の PCB（不一致なら 409）


class FlowCalibrationPatch(BaseModel):
    """PATCH /api/pasting/pad-config/flow-calibration のリクエスト.

    塗布量・crop 寸法・校正ファイルは machine 設定なので、ここでは扱わない。

    ``points`` は置き換えで、空の並びで未設定へ戻す。
    """

    points: list[list[float]] = []  # board 座標 [[x, y], ...]
    expected_pcb: str  # 編集開始時の PCB（不一致なら 409）


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
    applied_modes: dict[tuple[str, ...], AppliedModeSummary],
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
        height_fields=list(resolved[node.key].params.height_fields),
        own_override=own,
        own_summary=OwnSummary(**attrs.asdict(own_override_summary(setting))),
        descendant_summary=DescendantSummary(
            **attrs.asdict(descendant_override_summary(node, model))
        ),
        applied=(
            {"dispense_mode": AppliedValueInfo(**attrs.asdict(mode))}
            if (mode := applied_modes.get(node.key)) is not None
            else {}
        ),
        children=[
            tree(child, model, resolved, applied_modes) for child in node.children
        ],
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
            column=field.column,
        )
        for field in PASTE_PARAM_FIELDS
    ]


def _xy(point: Point2d | None) -> list[float] | None:
    return None if point is None else [point.x, point.y]


def build_initial_purge(plan: PastePlan) -> InitialPurgeInfo:
    """初回パージの設定と解決結果を返す（不正でも 400 にせず理由を ``error`` に載せる）."""
    view = initial_purge_view(plan)
    resolved = view.resolved
    return InitialPurgeInfo(
        initial_purge_ul=view.amount_ul,
        point=_xy(view.point),
        default_point=_xy(view.default_point),
        resolved=(
            None
            if resolved is None
            else ResolvedInitialPurgeInfo(
                label=resolved.label,
                amount=resolved.amount_ul,
                point=[resolved.point.x, resolved.point.y],
                source=resolved.source,
            )
        ),
        selection_label=view.selection_label,
        error=view.error,
    )


def build_flow_calibration(plan: PastePlan) -> FlowCalibrationInfo:
    """運転時補正の設定と解決結果を返す（不正でも 400 にせず理由を ``error`` に載せる）."""
    view = flow_calibration_view(plan)
    return FlowCalibrationInfo(
        enabled=view.enabled,
        points=[[point.x, point.y] for point in view.points],
        overlapping=list(view.overlapping),
        planned=view.planned,
        amount_ul=view.amount_ul,
        crop_size_mm=view.crop_size_mm,
        selection_label=view.selection_label,
        error=view.error,
    )


def pad_info(
    pad: Pad,
    package: str,
    node_ids: list[str],
    resolved: ResolvedSetting,
) -> PadInfo:
    return PadInfo(
        id=pad.ref,
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
def load_session(
    state: AppState, settings: Env, overrides: BoardOverrides
) -> BoardSession:
    """選択中基板の Board / 階層 / 保存済みモデルをまとめてロードする.

    Board はファイルが変わっていなければキャッシュを使い、差し替わっていれば読み直す。

    Raises:
        HTTPException: PCB 未選択（409）の場合
    """
    pcb_rel = state.selected_pcb
    if pcb_rel is None:
        raise HTTPException(status_code=409, detail="PCB が選択されていません")
    return BoardSession.load(
        overrides,
        pcb_rel.as_posix(),
        state.boards.get(settings.pcb_browse_root / pcb_rel),
        state.machine().paste_dispenser,
    )


def build_pad_config(loaded: BoardSession) -> PadConfigResponse:
    """ロード済みコンテキストから GET 形式のレスポンスを構築する."""
    pcb = loaded.board
    hierarchy = loaded.hierarchy
    model = loaded.model

    plan = plan_paste(loaded)
    resolved = plan.resolved
    node_resolved = resolve_node_settings(hierarchy, model)
    package_by_designator = {c.designator: c.package for c in pcb.components}

    pads = [
        pad_info(
            pad,
            package_by_designator.get(pad.designator, ""),
            [node_id(key) for key in hierarchy.node_keys_for_pad(pad)],
            resolved[pad.ref],
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
        initial_purge=build_initial_purge(plan),
        flow_calibration=build_flow_calibration(plan),
        tree=tree(
            hierarchy.root,
            model,
            node_resolved,
            applied_mode_summaries(
                hierarchy, resolved, node_resolved, loaded.base_config
            ),
        ),
        pads=pads,
        overrides=overrides(model),
        fields=param_fields(),
    )


def build_copper(loaded: BoardSession) -> PadConfigCopperResponse:
    """銅箔島を表示用に簡略化して返す（pad 編集ごとには取り直さない前提）."""
    islands = [
        CopperIslandInfo(layer=copper.layer.value, rings=rings)
        for copper in loaded.board.copper
        if (rings := display_rings(copper.polygon))
    ]
    return PadConfigCopperResponse(
        pcb_file=loaded.source_pcb,
        tolerance_mm=COPPER_DISPLAY_TOLERANCE_MM,
        islands=islands,
    )


def build_route(plan: PastePlan, layer: Layer) -> PasteRouteResponse:
    """有効 pad の塗布順路を返す."""
    route = [
        PasteRoutePad(
            id=stop.pad.ref,
            order=stop.order,
            group_label=stop.group_label,
            area=stop.area,
            center=[stop.pad.center.x, stop.pad.center.y],
        )
        for stop in plan.route
    ]
    return PasteRouteResponse(layer=layer.value, pads=route)


def build_fill_path(plan: PastePlan, layer: Layer) -> PasteFillPathResponse:
    """有効 pad の塗布経路を塗る順に返す."""
    pads: list[PasteFillPathPad] = []
    for pad in plan.routed_pads:
        fill = plan.fill_plan(pad)
        points = [[[point.x, point.y] for point in path] for path in fill.paths]
        pads.append(
            PasteFillPathPad(
                id=pad.ref,
                dispense_mode=fill.dispense_mode,
                path_count=len(points),
                point_count=sum(len(path) for path in points),
                paths=points,
            )
        )
    return PasteFillPathResponse(
        layer=layer.value,
        nozzle_diameter=plan.config.nozzle_diameter,
        pads=pads,
    )


def affected_pads(node: str, loaded: BoardSession) -> list[AffectedPad]:
    """指定ノード配下の全 pad を再解決して返す."""
    key = key_from_node_id(node)
    return _affected(loaded, lambda pad: key in loaded.hierarchy.node_keys_for_pad(pad))


def affected_pads_for_ids(
    ids: Iterable[str], loaded: BoardSession
) -> list[AffectedPad]:
    """指定 pad id 群を再解決して返す."""
    id_set = set(ids)
    return _affected(loaded, lambda pad: pad.ref in id_set)


def _affected(
    loaded: BoardSession, include: Callable[[Pad], bool]
) -> list[AffectedPad]:
    resolved = resolve_pad_settings(loaded.hierarchy, loaded.model)
    return [
        AffectedPad(
            id=pad.ref,
            enabled=paste.enabled,
            resolved=resolved_settings(paste),
        )
        for pad in loaded.hierarchy.iter_pads()
        if include(pad)
        for paste in (resolved[pad.ref],)
    ]
