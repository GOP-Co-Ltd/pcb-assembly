"""塗布タブの pad-config API エンドポイント（基板ごとの pad 有効/無効 + 階層 override）.

選択中の基板について、pad ジオメトリ・階層ツリー・解決済み塗布設定・
疎な override を 1 発で返し（GET）、ノード/pad 単位の編集を即時保存する
（PATCH）。契約モデルとレスポンス構築ヘルパ（node_id 規約を含む）は
:mod:`web.api.routers.pasting_view` に置く。
"""

from __future__ import annotations

from urllib.parse import quote

import attrs
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from pcbasm.geometry import Point2d
from pcbasm.pasting.initial_purge import validate_initial_purge
from pcbasm.pasting.params import validate_field_names, validate_param_values
from pcbasm.pasting.paste_volume.runtime import validate_flow_calibration_point
from pcbasm.pasting.persist import board_settings_export_filename
from pcbasm.pasting.route import routed_enabled_pads
from pcbasm.pasting.settings import PasteSettingsModel
from pcbasm.pcb import Layer
from web.api.dependencies import (
    BoardStoreDep,
    ControlDep,
    JobsDep,
    SettingsDep,
    StateDep,
    StoreDep,
)
from web.api.routers.pasting_view import (
    FlowCalibrationPatch,
    FlowCalibrationResponse,
    InitialPurgePatch,
    InitialPurgeResponse,
    Loaded,
    NodePatch,
    PadConfigCopperResponse,
    PadConfigImport,
    PadConfigResponse,
    PadEnablePatch,
    PasteFillPathRequest,
    PasteFillPathResponse,
    PasteRouteRequest,
    PasteRouteResponse,
    PatchResponse,
    TactEstimateResponse,
    affected_pads,
    affected_pads_for_ids,
    build_copper,
    build_fill_path,
    build_flow_calibration,
    build_initial_purge,
    build_pad_config,
    build_route,
    build_tact_estimate,
    key_from_node_id,
    layer_pads,
    load_board,
)

router = APIRouter(prefix="/api")


@router.get("/pasting/pad-config")
def get_pad_config(
    state: StateDep,
    settings: SettingsDep,
    board_store: BoardStoreDep,
) -> PadConfigResponse:
    """選択中基板の pad ジオメトリ・階層・解決済み設定・疎 override を返す."""
    return build_pad_config(load_board(state, settings, board_store))


@router.get("/pasting/pad-config/copper")
def get_pad_config_copper(
    state: StateDep,
    settings: SettingsDep,
    board_store: BoardStoreDep,
) -> PadConfigCopperResponse:
    """選択中基板の銅箔島を表示用に返す（基板ごとに 1 回取る読み取り専用）."""
    return build_copper(load_board(state, settings, board_store))


@router.post("/pasting/pad-config/route")
def calculate_pad_route(
    body: PasteRouteRequest,
    state: StateDep,
    settings: SettingsDep,
    board_store: BoardStoreDep,
) -> PasteRouteResponse:
    """選択中基板の有効 pad だけを対象に塗布順路を返す（POST だが読み取り専用計算）."""
    return build_route(load_board(state, settings, board_store), body.layer)


@router.post("/pasting/pad-config/fill-path")
def calculate_pad_fill_path(
    body: PasteFillPathRequest,
    state: StateDep,
    settings: SettingsDep,
    board_store: BoardStoreDep,
) -> PasteFillPathResponse:
    """選択中基板の有効 pad だけを対象に塗布パスを返す（POST だが読み取り専用計算）."""
    return build_fill_path(load_board(state, settings, board_store), body.layer)


@router.get("/pasting/tact-estimate")
def get_tact_estimate(
    state: StateDep,
    settings: SettingsDep,
    board_store: BoardStoreDep,
) -> TactEstimateResponse:
    """選択中基板のはんだ塗布タクトタイムを実行前に見積もる（読み取り専用）."""
    return build_tact_estimate(
        load_board(state, settings, board_store), state.machine().tact
    )


@router.patch("/pasting/pad-config/node")
def patch_pad_config_node(
    body: NodePatch,
    state: StateDep,
    settings: SettingsDep,
    board_store: BoardStoreDep,
    _control: ControlDep,
) -> PatchResponse:
    """ノードの enabled/values upsert・clear を適用し、影響 pad を返す."""
    loaded = load_board(state, settings, board_store)
    _check_expected_pcb(body.expected_pcb, loaded)
    if (message := validate_param_values(body.values)) is not None:
        raise HTTPException(status_code=400, detail=message)
    if (message := validate_field_names(body.clear)) is not None:
        raise HTTPException(status_code=400, detail=message)
    key = key_from_node_id(body.node)
    if key not in loaded.hierarchy.all_keys():
        raise HTTPException(status_code=400, detail=f"未知のノードです: {body.node}")
    enabled_sent = "enabled" in body.model_fields_set
    new_model = board_store.update(
        loaded.source_pcb,
        loaded.base_config,
        board_signature=loaded.board_signature,
        mutate=lambda current: current.with_level_patch(
            key,
            values=body.values,
            clear=body.clear,
            enabled=body.enabled,
            enabled_sent=enabled_sent,
        ),
    )
    updated = attrs.evolve(loaded, model=new_model)
    return PatchResponse(affected_pads=affected_pads(body.node, updated))


@router.patch("/pasting/pad-config/pads")
def patch_pad_config_pads(
    body: PadEnablePatch,
    state: StateDep,
    settings: SettingsDep,
    board_store: BoardStoreDep,
    _control: ControlDep,
) -> PatchResponse:
    """Pad id 配列を L4 ノードの enabled 設定として一括適用する."""
    loaded = load_board(state, settings, board_store)
    _check_expected_pcb(body.expected_pcb, loaded)
    l4_keys, unknown = loaded.hierarchy.l4_keys_for_pad_ids(body.ids)
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"未知の pad です: {', '.join(unknown)}"
        )
    model = board_store.update(
        loaded.source_pcb,
        loaded.base_config,
        board_signature=loaded.board_signature,
        mutate=lambda current: current.with_pads_enabled(l4_keys, enabled=body.enabled),
    )
    updated = attrs.evolve(loaded, model=model)
    return PatchResponse(affected_pads=affected_pads_for_ids(body.ids, updated))


@router.patch("/pasting/pad-config/initial-purge")
def patch_initial_purge(
    body: InitialPurgePatch,
    state: StateDep,
    settings: SettingsDep,
    board_store: BoardStoreDep,
    store: StoreDep,
    jobs: JobsDep,
    _control: ControlDep,
) -> InitialPurgeResponse:
    """初回パージ量と塗布座標を即時保存し、解決済み設定を返す."""
    loaded = load_board(state, settings, board_store)
    _check_expected_pcb(body.expected_pcb, loaded)
    amount_sent = "initial_purge_ul" in body.model_fields_set
    point_sent = "point" in body.model_fields_set
    if amount_sent and body.initial_purge_ul is None:
        raise HTTPException(
            status_code=400, detail="initial_purge_ulは数値で指定してください"
        )

    next_amount = (
        body.initial_purge_ul if amount_sent else loaded.base_config.initial_purge_ul
    )
    assert next_amount is not None
    next_point = (
        _initial_purge_point(body.point)
        if point_sent
        else loaded.model.initial_purge_point
    )
    error = validate_initial_purge(
        amount_ul=next_amount,
        point=next_point,
        outline=loaded.pcb.outline.polygon,
    )
    if error is not None:
        raise HTTPException(status_code=400, detail=error)

    if amount_sent:
        with state.machine_lock("pasting-initial-purge"):
            store.write_machine_settings(
                {"paste_dispenser.initial_purge_ul": next_amount}
            )
        jobs.publish_state_changed()
    model = loaded.model
    if point_sent:
        model = board_store.update(
            loaded.source_pcb,
            loaded.base_config,
            board_signature=loaded.board_signature,
            mutate=lambda current: current.with_initial_purge_point(next_point),
        )
    # PCB は再パースせず、machine.toml へ書いた分だけ base_config を読み直す
    updated = attrs.evolve(
        loaded,
        model=model,
        base_config=(
            state.machine().paste_dispenser if amount_sent else loaded.base_config
        ),
    )
    return InitialPurgeResponse(initial_purge=build_initial_purge(updated))


@router.patch("/pasting/pad-config/flow-calibration")
def patch_flow_calibration(
    body: FlowCalibrationPatch,
    state: StateDep,
    settings: SettingsDep,
    board_store: BoardStoreDep,
    _control: ControlDep,
) -> FlowCalibrationResponse:
    """運転時流量キャリブレーションの測定位置を即時保存し、解決済み設定を返す.

    塗布量・crop 寸法・校正ファイルは machine 設定なので
    ``PUT /api/settings/machine`` 側で扱う。
    ここは基板ごとの座標だけを持つ。

    ``points`` は置き換えで、空の並びが未設定。

    撮影範囲どうしの重なりはここでは撥ねない。
    crop 寸法を先に変えただけで保存できなくなるのを避けるため、判定は計画時に行い
    ``flow_calibration.error`` として返す。
    """
    loaded = load_board(state, settings, board_store)
    _check_expected_pcb(body.expected_pcb, loaded)
    if "points" not in body.model_fields_set:
        return FlowCalibrationResponse(flow_calibration=build_flow_calibration(loaded))

    next_points = _flow_calibration_points(body.points)
    for point in next_points:
        error = validate_flow_calibration_point(
            point=point, outline=loaded.pcb.outline.polygon
        )
        if error is not None:
            raise HTTPException(status_code=400, detail=error)
    model = board_store.update(
        loaded.source_pcb,
        loaded.base_config,
        board_signature=loaded.board_signature,
        mutate=lambda current: current.with_flow_calibration_points(next_points),
    )
    updated = attrs.evolve(loaded, model=model)
    return FlowCalibrationResponse(flow_calibration=build_flow_calibration(updated))


def _flow_calibration_points(values: list[list[float]]) -> tuple[Point2d, ...]:
    """API 入力の ``[[x, y], ...]`` を Point2d の並びへ正規化する."""
    for value in values:
        if len(value) != 2:
            raise HTTPException(
                status_code=400,
                detail="流量キャリブレーション位置は [x, y] の 2 要素で指定してください",
            )
    return tuple(Point2d(value[0], value[1]) for value in values)


def _check_expected_pcb(expected_pcb: str | None, loaded: Loaded) -> None:
    """編集開始時の PCB と選択中 PCB の不一致を 409 で弾く（None は無検査）."""
    if expected_pcb is not None and expected_pcb != loaded.source_pcb:
        raise HTTPException(
            status_code=409,
            detail="PCB が切り替わりました。ページを再読み込みしてください",
        )


def _initial_purge_point(value: list[float] | None) -> Point2d | None:
    """API 入力の ``[x, y]`` を Point2d へ正規化する（``None`` は指定解除）."""
    if value is None:
        return None
    if len(value) != 2:
        raise HTTPException(
            status_code=400, detail="パージ位置は [x, y] の 2 要素で指定してください"
        )
    return Point2d(value[0], value[1])


@router.get("/pasting/pad-config/export")
def export_pad_config(
    state: StateDep, settings: SettingsDep, board_store: BoardStoreDep
) -> JSONResponse:
    """現在の基板 override 設定をダウンロード用 JSON として返す."""
    loaded = load_board(state, settings, board_store)
    doc = board_store.export_doc(
        loaded.source_pcb,
        loaded.model,
        board_signature=loaded.board_signature,
    )
    filename = board_settings_export_filename(loaded.source_pcb)
    return JSONResponse(
        content=doc,
        headers={"Content-Disposition": _attachment(filename)},
    )


def _attachment(filename: str) -> str:
    """``Content-Disposition`` を組む（日本語基板名は RFC 5987 の ``filename*`` に載せる）.

    ヘッダ値は latin-1 しか運べないので、``filename=`` には ASCII 化した控えを置く。
    """
    fallback = filename.encode("ascii", "replace").decode("ascii").replace('"', "_")
    return (
        f'attachment; filename="{fallback}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


@router.post("/pasting/pad-config/import")
def import_pad_config(
    body: PadConfigImport,
    state: StateDep,
    settings: SettingsDep,
    board_store: BoardStoreDep,
    _control: ControlDep,
) -> PadConfigResponse:
    """アップロードされた基板 override 設定を検証して保存し、最新設定を返す."""
    loaded = load_board(state, settings, board_store)
    try:
        model = board_store.model_from_doc(
            body.document,
            loaded.base_config,
            board_signature=loaded.board_signature,
            expected_source_pcb=loaded.source_pcb,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    pruned = board_store.prune(
        loaded.source_pcb,
        model,
        loaded.hierarchy,
        outline=loaded.pcb.outline.polygon,
        board_signature=loaded.board_signature,
    )
    return build_pad_config(attrs.evolve(loaded, model=pruned))
