"""塗布タブの pad-config API エンドポイント（基板ごとの pad 有効/無効 + 階層 override）.

選択中の基板について、pad ジオメトリ・階層ツリー・解決済み塗布設定・
疎な override を 1 発で返し（GET）、ノード/pad 単位の編集を即時保存する
（PATCH）。契約モデルとレスポンス構築ヘルパ（node_id 規約を含む）は
:mod:`webui.routers.pasting_view` に置く。
"""

from __future__ import annotations

import attrs
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from pcbasm.pasting import (
    routed_enabled_pads,
    validate_field_names,
    validate_initial_purge,
    validate_override_values,
)
from pcbasm.pcb import Layer
from webui.dependencies import BoardStoreDep, JobsDep, SettingsDep, StateDep, StoreDep
from webui.routers.pasting_view import (
    InitialPurgePatch,
    InitialPurgeResponse,
    NodePatch,
    PadConfigImport,
    PadConfigResponse,
    PadEnablePatch,
    PasteFillPathRequest,
    PasteFillPathResponse,
    PasteRouteRequest,
    PasteRouteResponse,
    PatchResponse,
    affected_pads,
    affected_pads_for_ids,
    build_fill_path,
    build_initial_purge,
    build_pad_config,
    build_route,
    key_from_node_id,
    layer_pads,
    load_board,
)

router = APIRouter(prefix="/api")


@router.get("/pasting/pad-config")
def get_pad_config(
    state: StateDep, settings: SettingsDep, board_store: BoardStoreDep
) -> PadConfigResponse:
    """選択中基板の pad ジオメトリ・階層・解決済み設定・疎 override を返す."""
    return build_pad_config(load_board(state, settings, board_store))


@router.post("/pasting/pad-config/route")
def calculate_pad_route(
    body: PasteRouteRequest,
    state: StateDep,
    settings: SettingsDep,
    board_store: BoardStoreDep,
) -> PasteRouteResponse:
    """選択中基板の有効 pad だけを対象に塗布順路を返す."""
    return build_route(load_board(state, settings, board_store), body.layer)


@router.post("/pasting/pad-config/fill-path")
def calculate_pad_fill_path(
    body: PasteFillPathRequest,
    state: StateDep,
    settings: SettingsDep,
    board_store: BoardStoreDep,
) -> PasteFillPathResponse:
    """選択中基板の有効 pad だけを対象に塗布パスを返す."""
    return build_fill_path(load_board(state, settings, board_store), body.layer)


@router.patch("/pasting/pad-config/node")
def patch_pad_config_node(
    body: NodePatch,
    state: StateDep,
    settings: SettingsDep,
    board_store: BoardStoreDep,
) -> PatchResponse:
    """ノードの enabled/values upsert・clear を適用し、影響 pad を返す."""
    loaded = load_board(state, settings, board_store)
    if (message := validate_override_values(body.values)) is not None:
        raise HTTPException(status_code=400, detail=message)
    if (message := validate_field_names(body.clear)) is not None:
        raise HTTPException(status_code=400, detail=message)
    key = key_from_node_id(body.node)
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
        loaded.source_pcb,
        new_model,
        board_signature=loaded.board_signature,
    )
    updated = attrs.evolve(loaded, model=new_model)
    return PatchResponse(affected_pads=affected_pads(body.node, updated))


@router.patch("/pasting/pad-config/pads")
def patch_pad_config_pads(
    body: PadEnablePatch,
    state: StateDep,
    settings: SettingsDep,
    board_store: BoardStoreDep,
) -> PatchResponse:
    """Pad id 配列を L4 ノードの enabled 設定として一括適用する."""
    loaded = load_board(state, settings, board_store)
    l4_keys, unknown = loaded.hierarchy.l4_keys_for_pad_ids(body.ids)
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"未知の pad です: {', '.join(unknown)}"
        )
    model = loaded.model.with_pads_enabled(l4_keys, enabled=body.enabled)
    board_store.save(
        loaded.source_pcb,
        model,
        board_signature=loaded.board_signature,
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
) -> InitialPurgeResponse:
    """初回パージ量と pad 指定を即時保存し、解決済み設定を返す."""
    loaded = load_board(state, settings, board_store)
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
        layer_pads(loaded, Layer.TOP.value), loaded.hierarchy, loaded.model
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
            store.write_machine_settings(
                {"paste_dispenser.initial_purge_ul": next_amount}
            )
        jobs.publish_state_changed()
    if pad_sent:
        model = loaded.model.with_initial_purge_pad_id(next_pad_id)
        board_store.save(
            loaded.source_pcb,
            model,
            board_signature=loaded.board_signature,
        )
    return InitialPurgeResponse(
        initial_purge=build_initial_purge(load_board(state, settings, board_store))
    )


def _normalize_initial_purge_pad_id(pad_id: str | None) -> str | None:
    """API 入力の空文字を未指定へ正規化する."""
    return None if pad_id in (None, "") else pad_id


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
        board_signature=loaded.board_signature,
    )
    return build_pad_config(attrs.evolve(loaded, model=pruned))
