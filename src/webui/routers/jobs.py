"""ジョブの REST API と WebSocket（/api/ws）."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, cast

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from webui.app import CatalogDep, JobsDep, SettingsDep, StateDep, StoreDep
from webui.jobs.catalog import JobCatalog, JobDefinition
from webui.jobs.manager import JobManager, JobRecord, prompt_payload
from webui.models import (
    ApplyInfo,
    ArtifactInfo,
    JobResultInfo,
    JobStatusName,
    JobSummary,
    PromptInfo,
)

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
        apply_available=record.apply_available,
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
    machine = state.selected_machine
    with state.machine_lock("apply-settings"):
        store.write_machine_settings(machine, dict(payload.values))
        configs_dir = store.machine_toml_path(machine).parent
        for file in payload.files:
            (configs_dir / file.filename).write_bytes(file.content)
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
    await websocket.accept()
    events = jobs.subscribe()
    try:
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
