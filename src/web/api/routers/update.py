"""WebUI からのソフトウェア更新（git pull → uv sync → サービス再起動）の API.

ジョブ基盤には載せない。`JobManager.start()` は `uses_machine` に関係なく必ず
装置ロックを取り、job record は非永続なので再起動でプロセスが終了すると更新の記録が
消える。独立エンドポイント + 明示的な busy チェックにしてある。

判断はすべて `web.selfupdate` 側にあり、この router は入出力変換だけを行う
（`webui-thin-wrapper`）。**参照先（ブランチ / remote / ref / `uv` の引数）は
リクエストで一切指定できない。** 指定できるようにすると「LAN から任意コード実行」が可能になる。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from web.api.dependencies import ControlDep, SettingsDep, StateDep, UpdateDep
from web.api.models import UpdateRunResponse, UpdateStatusResponse
from web.api.settings import resolve_machine_id
from web.selfupdate.report import UpdatePlan, run_payload, status_payload
from web.selfupdate.runner import UpdateRunner

router = APIRouter(prefix="/api")


class UpdateRunRequest(BaseModel):
    """更新の実行要求.

    `expected_head` だけを受け取る（画面が見ていた HEAD との楽観ロック）。開きっぱなしの
    古いタブや `curl` の単発リクエストを拒否するためのもので、**認証ではない**。
    """

    expected_head: str


def _status(
    update: UpdateRunner, plan: UpdatePlan, settings: SettingsDep
) -> UpdateStatusResponse:
    return status_payload(plan, update.status(), hostname=resolve_machine_id(settings))


@router.get("/update/status")
def get_update_status(update: UpdateDep, settings: SettingsDep) -> UpdateStatusResponse:
    """更新の可否と直近の結果（fetch しないので毎秒ポーリングしてよい）."""
    return _status(update, update.plan(), settings)


@router.post("/update/check")
def post_update_check(
    update: UpdateDep, settings: SettingsDep, _control: ControlDep
) -> UpdateStatusResponse:
    """Remote を fetch してから状態を返す（ネットワーク I/O を伴う）."""
    return _status(update, update.check(), settings)


@router.post("/update/run", status_code=202)
def post_update_run(
    payload: UpdateRunRequest,
    update: UpdateDep,
    state: StateDep,
    _control: ControlDep,
) -> UpdateRunResponse:
    """更新を開始する（受理は 202。処理はバックグラウンドで進む）.

    Raises:
        HTTPException: 無効化されていれば 403、装置が使用中 / 実行中 / 中断事由あり /
            `expected_head` 不一致なら 409
    """
    if not update.enabled:
        raise HTTPException(
            status_code=403, detail="この機体では WebUI からの更新が無効です。"
        )
    # 全ジョブが装置ロックを取るので、これで塗布中もジョブ中も拒否できる
    if state.busy_owner is not None:
        raise HTTPException(
            status_code=409,
            detail=f"装置が使用中です（{state.busy_owner}）。終わってからやり直してください。",
        )
    run_id, reason = update.start(expected_head=payload.expected_head)
    if reason is not None:
        raise HTTPException(status_code=409, detail=reason)
    return UpdateRunResponse(run_id=run_id or "", run=run_payload(update.status()))
