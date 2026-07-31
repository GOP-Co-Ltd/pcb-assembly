"""ジョブの REST API と WebSocket（/api/ws）."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, cast

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from web.api.dependencies import CatalogDep, JobsDep, SettingsDep, StateDep, StoreDep
from web.api.jobs.catalog import JobCatalog, JobDefinition
from web.api.jobs.manager import JobManager, JobRecord, prompt_payload
from web.api.models import (
    ApplyInfo,
    ArtifactInfo,
    JobCatalogResponse,
    JobResultInfo,
    JobSpecInfo,
    JobStatusName,
    JobSummary,
    ParamSpecInfo,
    PromptInfo,
)
from web.api.routers.common import param_specs_with_saved_defaults
from web.api.state import AppState

router = APIRouter(prefix="/api")


class JobStartRequest(BaseModel):
    params: dict[str, Any] = {}


def job_summary(record: JobRecord, definition: JobDefinition) -> JobSummary:
    """JobRecord から JobSummary を構築する（REST / WS 共通の唯一の変換点）."""
    pending = record.pending_prompt
    result = record.result
    result_info = None
    if result is not None:
        result_info = JobResultInfo(
            summary=result.summary,
            artifacts=[
                ArtifactInfo(
                    label=artifact.label,
                    url=f"/artifacts/{artifact.path}",
                    kind=artifact.kind,
                )
                for artifact in result.artifacts
            ],
            apply=(
                ApplyInfo(label=result.apply.label, values=dict(result.apply.values))
                if result.apply is not None
                else None
            ),
        )
    return JobSummary(
        id=record.id,
        name=record.name,
        label=definition.label,
        status=cast(JobStatusName, record.status.value),
        params=dict(record.params),
        error=record.error,
        progress_stage=record.progress_stage,
        progress_percent=record.progress_percent,
        log_tail=list(record.log_lines),
        pending_prompt=(
            PromptInfo.model_validate(prompt_payload(pending[0], pending[1]))
            if pending is not None
            else None
        ),
        result=result_info,
        accepts_commands=definition.accepts_commands,
        notify_on_completion=definition.notify_on_completion,
        apply_available=record.apply_available,
    )


def job_spec_info(
    definition: JobDefinition, state: AppState, catalog: JobCatalog
) -> JobSpecInfo:
    """JobDefinition を公開表現へ変換する（params は保存済み既定値を反映）."""
    return JobSpecInfo(
        name=definition.name,
        label=definition.label,
        tab=definition.tab,
        params=[
            ParamSpecInfo(
                name=spec.name,
                label=spec.label,
                value_type=spec.value_type,
                default=spec.default,
                choices=list(spec.choices),
                unit=spec.unit,
                help=spec.help,
                runtime_editable=spec.runtime_editable,
                minimum=spec.minimum,
                optional=spec.optional,
            )
            for spec in param_specs_with_saved_defaults(definition, state, catalog)
        ],
        requires_pcb=definition.requires_pcb,
        uses_machine=definition.uses_machine,
        notify_on_completion=definition.notify_on_completion,
        accepts_commands=definition.accepts_commands,
        persisted_params=list(definition.persisted_params),
        runtime_params=list(definition.runtime_params),
        hidden=definition.hidden,
        provides_preview=definition.provides_preview,
        loading_param=definition.loading_param,
        loading_stages=definition.loading_stages,
    )


@router.get("/jobs")
def get_jobs(state: StateDep, catalog: CatalogDep) -> JobCatalogResponse:
    """登録済みジョブ定義を登録順で全件返す（hidden も含む）.

    hidden ジョブも実行時登録され POST できるため、frontend が扱えるよう filter しない。
    """
    return JobCatalogResponse(
        jobs=[
            job_spec_info(definition, state, catalog) for definition in catalog.list()
        ]
    )


@router.post("/jobs/{name}", status_code=201)
def post_job(
    name: str, jobs: JobsDep, catalog: CatalogDep, body: JobStartRequest | None = None
) -> dict[str, JobSummary]:
    """ジョブを開始する（404: 未知ジョブ / 400: パラメータ不正 / 409: 実行中）."""
    values = body.params if body is not None else {}
    try:
        record = jobs.start(name, values)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # BusyError は app.py の例外ハンドラが 409 化する
    return {"job": job_summary(record, catalog.get(name))}


class JobParamDefaultsRequest(BaseModel):
    """フォーム入力の即保存リクエスト."""

    values: dict[str, Any] = {}


@router.post("/jobs/{name}/param-defaults")
def post_job_param_defaults(
    name: str,
    catalog: CatalogDep,
    state: StateDep,
    body: JobParamDefaultsRequest | None = None,
) -> dict[str, dict[str, bool | float | int | str]]:
    """フォーム入力を「実行」を待たずに次回フォーム既定値として保存する.

    入力時の即保存用（404: 未知ジョブ）。``persisted_params`` のうち型整合する値だけを
    既存の保存済み既定値へマージする。入力途中の空欄・型不一致・persisted 外のキーは
    無視する（実行前なのでエラーにしない）。
    """
    values = body.values if body is not None else {}
    try:
        definition = catalog.get(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    merged = state.merge_job_param_defaults(
        name, catalog.filter_persisted_defaults(definition, values)
    )
    return {"defaults": merged}


@router.get("/jobs/current")
def get_current_job(jobs: JobsDep, catalog: CatalogDep) -> dict[str, JobSummary | None]:
    """直近ジョブの全量サマリ（WS 再接続時の同期用。無ければ null）."""
    record = jobs.current()
    if record is None:
        return {"job": None}
    return {"job": job_summary(record, catalog.get(record.name))}


@router.post("/jobs/current/abort")
def post_abort(jobs: JobsDep) -> dict[str, bool]:
    """実行中ジョブへ協調的中止を要求する（409: アクティブジョブ無し）."""
    if not jobs.request_abort():
        raise HTTPException(status_code=409, detail="実行中のジョブがありません")
    return {"aborted": True}


class JobParamsUpdateRequest(BaseModel):
    """実行中パラメータ編集リクエスト（runtime_editable な subset の置換）."""

    values: dict[str, Any] = {}
    persist: bool = False


@router.put("/jobs/current/params")
def put_current_params(jobs: JobsDep, body: JobParamsUpdateRequest) -> dict[str, Any]:
    """実行中ジョブの runtime_editable パラメータを即時更新する（400: 不正）."""
    try:
        updated = jobs.update_current_params(body.values, persist=body.persist)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"params": updated}


@router.post("/jobs/last/apply")
def post_apply(
    jobs: JobsDep, state: StateDep, store: StoreDep, settings: SettingsDep
) -> dict[str, dict[str, bool | float | int | str]]:
    """直近 SUCCEEDED ジョブの計測結果を設定へ反映する.

    409: 反映可能なジョブ無し（LookupError）/ ジョブ実行中（BusyError）。
    400: ホワイトリスト外キー（UnknownFieldError）。
    """
    try:
        payload = jobs.apply_payload()
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    with state.machine_lock("apply-settings"):
        store.write_machine_settings(dict(payload.values))
        config_dir = store.machine_toml_path().parent
        for file in payload.files:
            (config_dir / file.filename).write_bytes(file.content)
    jobs.mark_applied()
    jobs.publish_state_changed()
    return {"applied": dict(payload.values)}


@router.post("/jobs/last/discard")
def post_discard(jobs: JobsDep) -> dict[str, bool]:
    """直近ジョブの設定反映ペイロードを破棄する（冪等）."""
    jobs.discard()
    return {"ok": True}


@router.websocket("/ws")
async def jobs_websocket(websocket: WebSocket) -> None:
    """グローバル 1 本のイベント / コマンドチャネル.

    サーバー → クライアント: job_status / log / progress / prompt /
    prompt_resolved / state_changed / error。 クライアント → サーバー:
    respond_prompt / command / abort。
    """
    jobs: JobManager = websocket.app.state.jobs
    catalog: JobCatalog = websocket.app.state.catalog
    # accept 前に subscribe し「接続完了 ↔ 購読開始」の取りこぼし窓を無くす
    events = jobs.subscribe()
    try:
        await websocket.accept()
        sender = asyncio.create_task(_send_loop(websocket, jobs, catalog, events))
        receiver = asyncio.create_task(_receive_loop(websocket, jobs, events))
        done, pending = await asyncio.wait(
            {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
                await task
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                raise exc
    finally:
        jobs.unsubscribe(events)


async def _send_loop(
    websocket: WebSocket,
    jobs: JobManager,
    catalog: JobCatalog,
    events: asyncio.Queue[dict[str, Any]],
) -> None:
    """購読キューのイベントを送信する（job_status は送信時に最新化）."""
    while True:
        event = await events.get()
        if event.get("type") == "job_status":
            record = jobs.current()
            if record is None or record.id != event.get("job_id"):
                continue  # 新ジョブに置き換わった後の旧イベントは捨てる
            event = {
                "type": "job_status",
                "job": job_summary(record, catalog.get(record.name)).model_dump(
                    mode="json"
                ),
            }
        await websocket.send_json(event)


async def _receive_loop(
    websocket: WebSocket, jobs: JobManager, events: asyncio.Queue[dict[str, Any]]
) -> None:
    """クライアントメッセージを処理する（不正は error イベントで応答）.

    送信は _send_loop に一本化するため、error は購読キューへ直接積む。
    """
    while True:
        try:
            message = await websocket.receive_json()
        except ValueError:  # json.JSONDecodeError を含む
            events.put_nowait({"type": "error", "detail": "不正な JSON です"})
            continue
        try:
            _dispatch(message, jobs)
        except (ValueError, KeyError) as exc:
            events.put_nowait({"type": "error", "detail": str(exc)})


def _dispatch(message: dict[str, Any], jobs: JobManager) -> None:
    """クライアントメッセージ 1 件を JobManager へ振り分ける.

    Raises:
        ValueError: 未知 type・必須フィールド欠落・JobManager の検証エラー
    """
    match message.get("type"):
        case "respond_prompt":
            jobs.respond_prompt(str(message.get("prompt_id")), message.get("answer"))
        case "command":
            command = message.get("command")
            if not isinstance(command, dict):
                raise ValueError("command オブジェクトが必要です")
            jobs.submit_command(cast(dict[str, Any], command))
        case "abort":
            jobs.request_abort()
        case unknown:
            raise ValueError(f"未知のメッセージ type です: {unknown!r}")
