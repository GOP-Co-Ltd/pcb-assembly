"""ジョブの REST API と WebSocket（/api/jobs/ws）."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping
from typing import Any, cast

import attrs
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from pcbasm.validators import is_finite_number
from web.api.control import (
    ClientIdentity,
    ControlDeniedError,
    ControlLease,
    control_payload,
)
from web.api.deps import (
    CatalogDep,
    ControlDep,
    JobsDep,
    LockDep,
    MachineConfigDep,
    SelectionDep,
    get_backend,
)
from web.api.identity import get_identity
from web.api.jobs.runtime.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.runtime.context import prompt_payload
from web.api.jobs.runtime.manager import JobManager
from web.api.jobs.runtime.record import JobRecord
from web.api.jobs.steps import LoadingPanel
from web.api.selection import Selection
from web.contract.models import (
    ApplyInfo,
    ArtifactInfo,
    JobCatalogResponse,
    JobResultInfo,
    JobSpecInfo,
    JobStatusName,
    JobSummary,
    LoadingPanelInfo,
    LoadingRotationInfo,
    ParamSpecInfo,
    PromptInfo,
)

router = APIRouter(prefix="/api/jobs")


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
        elapsed_seconds=record.elapsed_seconds,
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


def _param_specs_with_saved_defaults(
    definition: JobDefinition, selection: Selection, catalog: JobCatalog
) -> tuple[ParamSpec, ...]:
    """保存済み既定値を ParamSpec の default に反映する.

    型判定・coerce は :meth:`JobCatalog.filter_persisted_defaults` に一本化する
    （persisted_params 外・型不一致はエラーにせず除外 = spec 既定値のまま）。
    """
    saved = selection.job_param_defaults(definition.name)
    if not saved or not definition.persisted_params:
        return definition.params
    valid = catalog.filter_persisted_defaults(definition, saved)
    return tuple(
        attrs.evolve(spec, default=valid[spec.name]) if spec.name in valid else spec
        for spec in definition.params
    )


def _loading_panel_info(
    panel: LoadingPanel, specs: Mapping[str, ParamSpec]
) -> LoadingPanelInfo:
    """ローディング欄の宣言を、入力欄の既定値を引いた公開表現にする."""

    def default(name: str) -> float:
        value = specs[name].default
        assert is_finite_number(value)  # ローディング欄の既定値は数値の ParamSpec
        return float(value)

    rotation = panel.rotation
    return LoadingPanelInfo(
        amount=default(panel.amount),
        rotation=(
            None
            if rotation is None
            else LoadingRotationInfo(
                rotations=default(rotation.rotations),
                rate=default(rotation.rate),
                accel=default(rotation.accel),
                retract_rotations=default(rotation.retract_rotations),
            )
        ),
        stages=list(panel.stages),
    )


def job_spec_info(
    definition: JobDefinition, selection: Selection, catalog: JobCatalog
) -> JobSpecInfo:
    """JobDefinition を公開表現へ変換する（params とローディング欄は保存済み既定値を反映）."""
    specs = _param_specs_with_saved_defaults(definition, selection, catalog)
    return JobSpecInfo(
        name=definition.name,
        label=definition.label,
        params=[
            ParamSpecInfo.model_validate(spec, from_attributes=True) for spec in specs
        ],
        requires_pcb=definition.requires_pcb,
        uses_machine=definition.uses_machine,
        notify_on_completion=definition.notify_on_completion,
        accepts_commands=definition.accepts_commands,
        persisted_params=list(definition.persisted_params),
        runtime_params=list(definition.runtime_params),
        provides_preview=definition.provides_preview,
        loading=(
            None
            if definition.loading is None
            else _loading_panel_info(
                definition.loading, {spec.name: spec for spec in specs}
            )
        ),
    )


@router.get("")
def get_jobs(selection: SelectionDep, catalog: CatalogDep) -> JobCatalogResponse:
    """登録済みジョブ定義を登録順で全件返す."""
    return JobCatalogResponse(
        jobs=[
            job_spec_info(definition, selection, catalog)
            for definition in catalog.list()
        ]
    )


@router.post("/{name}", status_code=201)
def post_job(
    name: str,
    jobs: JobsDep,
    catalog: CatalogDep,
    _control: ControlDep,
    body: JobStartRequest | None = None,
) -> dict[str, JobSummary]:
    """ジョブを開始する（404: 未知ジョブ / 400: パラメータ不正 / 409: 実行中）."""
    values = body.params if body is not None else {}
    try:
        record = jobs.start(name, values)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # BusyError は web.api.errors が 409 化する
    return {"job": job_summary(record, catalog.get(name))}


class JobParamDefaultsRequest(BaseModel):
    """フォーム入力の即保存リクエスト."""

    values: dict[str, Any] = {}


@router.post("/{name}/param-defaults")
def post_job_param_defaults(
    name: str,
    catalog: CatalogDep,
    selection: SelectionDep,
    _control: ControlDep,
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
    merged = selection.merge_job_param_defaults(
        name, catalog.filter_persisted_defaults(definition, values)
    )
    return {"defaults": merged}


@router.get("/current")
def get_current_job(jobs: JobsDep, catalog: CatalogDep) -> dict[str, JobSummary | None]:
    """直近ジョブの全量サマリ（WS 再接続時の同期用。無ければ null）."""
    record = jobs.current()
    if record is None:
        return {"job": None}
    return {"job": job_summary(record, catalog.get(record.name))}


@router.post("/current/abort")
def post_abort(jobs: JobsDep) -> dict[str, bool]:
    """実行中ジョブへ協調的中止を要求する（409: アクティブジョブ無し）.

    安全機能なので操作権でゲートしない（**ControlDep を足さない**）。
    """
    if not jobs.request_abort():
        raise HTTPException(status_code=409, detail="実行中のジョブがありません")
    return {"aborted": True}


class JobParamsUpdateRequest(BaseModel):
    """実行中パラメータ編集リクエスト（runtime_editable な subset の置換）."""

    values: dict[str, Any] = {}
    persist: bool = False
    expected_job_id: str


@router.put("/current/params")
def put_current_params(
    jobs: JobsDep, body: JobParamsUpdateRequest, _control: ControlDep
) -> dict[str, Any]:
    """実行中パラメータを即時更新する（400: 不正 / 409: 対象ジョブ不一致）."""
    try:
        updated = jobs.update_current_params(
            body.values, persist=body.persist, expected_job_id=body.expected_job_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"params": updated}


class JobResultRequest(BaseModel):
    """画面で確認したジョブの結果だけを反映・破棄する."""

    expected_job_id: str


@router.post("/last/apply")
def post_apply(
    jobs: JobsDep,
    lock: LockDep,
    machine_config: MachineConfigDep,
    _control: ControlDep,
    body: JobResultRequest,
) -> dict[str, dict[str, bool | float | int | str]]:
    """直近 SUCCEEDED ジョブの計測結果を設定へ反映する.

    409: 対象ジョブ不一致・反映可能なジョブ無し（LookupError）/ 実行中（BusyError）。
    400: 未知のキー・不正な値（InvalidSettingError）。
    """
    with lock.hold("apply-settings"):
        try:
            payload = jobs.apply_payload(expected_job_id=body.expected_job_id)
        except LookupError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        machine_config.write(
            payload.values,
            files={file.filename: file.content for file in payload.files},
        )
        jobs.mark_applied(expected_job_id=body.expected_job_id)
    return {"applied": dict(payload.values)}


@router.post("/last/discard")
def post_discard(
    jobs: JobsDep,
    _control: ControlDep,
    body: JobResultRequest,
) -> dict[str, bool]:
    """直近ジョブの設定反映ペイロードを破棄する（冪等。409: 対象ジョブ不一致）."""
    try:
        jobs.discard(expected_job_id=body.expected_job_id)
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@router.websocket("/ws")
async def jobs_websocket(websocket: WebSocket) -> None:
    """グローバル 1 本のイベント / コマンドチャネル.

    サーバーからクライアントへ送るイベントは次の 8 種である。

    - job_status / log / progress / prompt / prompt_resolved
    - state_changed / control_changed / error

    クライアントからは respond_prompt / command / abort を受ける。

    respond_prompt と command は操作権が必要で、abort は不要である。

    操作権が無いときは HTTP の 423 ではなく error イベントで断る。

    接続は誰にでも許す（閲覧は自由）。

    WS の在線は操作権リースの liveness なので、accept 前に登録する。

    subscribe と同じく、登録は finally で必ず解除する。
    """
    backend = get_backend(websocket)
    jobs = backend.jobs
    catalog = backend.catalog
    lease = backend.control
    identity = get_identity(websocket)
    # accept 前に subscribe し「接続完了 ↔ 購読開始」の取りこぼし窓を無くす
    # （control_changed も同じキューへ届く = EventBus.publish が唯一の配布点）
    events = backend.events.subscribe()
    lease.connect(identity)
    try:
        await websocket.accept()
        sender = asyncio.create_task(
            _send_loop(websocket, jobs, catalog, events, lease, identity)
        )
        receiver = asyncio.create_task(
            _receive_loop(websocket, jobs, events, lease, identity)
        )
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
        lease.disconnect(identity)
        backend.events.unsubscribe(events)


async def _send_loop(
    websocket: WebSocket,
    jobs: JobManager,
    catalog: JobCatalog,
    events: asyncio.Queue[dict[str, Any]],
    lease: ControlLease,
    identity: ClientIdentity,
) -> None:
    """購読キューのイベントを送信する.

    job_status は送信時に最新化し、control_changed にはこの接続から見た操作権の状態を詰める。
    """
    while True:
        event = await events.get()
        match event.get("type"):
            case "job_status":
                record = jobs.current()
                if record is None or record.id != event.get("job_id"):
                    continue  # 新ジョブに置き換わった後の旧イベントは捨てる
                event = {
                    "type": "job_status",
                    "job": job_summary(record, catalog.get(record.name)).model_dump(
                        mode="json"
                    ),
                }
            case "control_changed":
                event = {
                    "type": "control_changed",
                    "control": control_payload(lease.snapshot(), identity).model_dump(
                        mode="json"
                    ),
                }
        await websocket.send_json(event)


async def _receive_loop(
    websocket: WebSocket,
    jobs: JobManager,
    events: asyncio.Queue[dict[str, Any]],
    lease: ControlLease,
    identity: ClientIdentity,
) -> None:
    """クライアントメッセージを処理する（不正は error イベントで応答）.

    送信は _send_loop に一本化するため、error は購読キューへ直接積む。
    `ControlDeniedError` も同じ経路で返す。ここで捕捉しないと例外が `asyncio.wait` を
    抜けて WS が切断され、クライアントが再接続ループに入る（FastAPI の
    `exception_handler` は WebSocket には適用されない）。
    """
    while True:
        try:
            message = await websocket.receive_json()
        except ValueError:  # json.JSONDecodeError を含む
            events.put_nowait({"type": "error", "detail": "不正な JSON です"})
            continue
        try:
            if not isinstance(message, dict):
                raise ValueError("メッセージには JSON オブジェクトが必要です")
            _dispatch(message, jobs, lease, identity)
        except (ValueError, LookupError, ControlDeniedError) as exc:
            events.put_nowait({"type": "error", "detail": str(exc)})


def _dispatch(
    message: dict[str, Any],
    jobs: JobManager,
    lease: ControlLease,
    identity: ClientIdentity,
) -> None:
    """クライアントメッセージ 1 件を JobManager へ振り分ける.

    `abort` は安全機能なので操作権を要求しない（**リース保持者が応答できない状態でも
    誰かが必ず止められる**ようにするため）。

    Raises:
        ValueError: 未知 type・必須フィールド欠落・JobManager の検証エラー
        ControlDeniedError: 他クライアントが操作権を保持している場合
    """
    match message.get("type"):
        case "respond_prompt":
            lease.claim(identity)
            jobs.respond_prompt(str(message.get("prompt_id")), message.get("answer"))
        case "command":
            command = message.get("command")
            if not isinstance(command, dict):
                raise ValueError("command オブジェクトが必要です")
            expected_job_id = message.get("expected_job_id")
            if not isinstance(expected_job_id, str):
                raise ValueError("expected_job_id（文字列）が必要です")
            lease.claim(identity)
            jobs.submit_command(
                cast(dict[str, Any], command), expected_job_id=expected_job_id
            )
        case "abort":
            jobs.request_abort()
        case unknown:
            raise ValueError(f"未知のメッセージ type です: {unknown!r}")
