"""Frontend 自身のソフトウェア更新（`/update` ページとその API）.

frontend 専用機（`pcbasm-ui.service` だけが動き backend が居ないホスト）には
backend のジョブ基盤も操作権も無いので、frontend が自前の更新 API を持つ。
`machines_api.py` が先例。

パスは backend と**意図的に変える**（`/m/{id}/api/update/**` は proxy で backend 行き
なので、frontend 自身の入口は `/api/self-update/**`）。`pages.router` の `/{tab}`
キャッチオールに食われないよう、アプリでは pages より**先に**登録する。

frontend には操作権（control lease）が無い。多層の安全弁で守るが、**いずれも認証では
ない**（LAN に居る者は誰でも押せる）:

1. `expected_head` 必須の 2 段階（楽観ロック）
2. 単一実行ロック（flock。同居機の backend とも共有する）
3. `enabled` 設定（env で無効化。無効時は 403 + ボタン非表示）
4. ブラウザの `confirm()` にホスト名と再起動する unit を出す
"""

from __future__ import annotations

import socket

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from web.api.models import UpdateRunResponse, UpdateStatusResponse
from web.selfupdate.report import UpdatePlan, run_payload, status_payload
from web.selfupdate.runner import UpdateRunner
from web.ui import pages

router = APIRouter()


class SelfUpdateRunRequest(BaseModel):
    """更新の実行要求（受け取るのは楽観ロックの `expected_head` だけ）."""

    expected_head: str


def _runner(request: Request) -> UpdateRunner:
    runner: UpdateRunner = request.app.state.update
    return runner


def _status(runner: UpdateRunner, plan: UpdatePlan) -> UpdateStatusResponse:
    return status_payload(plan, runner.status(), hostname=socket.gethostname())


@router.get("/api/self-update")
def get_self_update(request: Request) -> UpdateStatusResponse:
    """更新の可否と直近の結果（fetch しない。毎秒ポーリングされる）."""
    runner = _runner(request)
    return _status(runner, runner.plan())


@router.post("/api/self-update/check")
def post_self_update_check(request: Request) -> UpdateStatusResponse:
    """Remote を fetch してから状態を返す."""
    runner = _runner(request)
    return _status(runner, runner.check())


@router.post("/api/self-update/run", status_code=202)
def post_self_update_run(
    payload: SelfUpdateRunRequest, request: Request
) -> UpdateRunResponse:
    """Frontend 自身の更新を開始する（受理は 202）.

    Raises:
        HTTPException: 無効化されていれば 403、実行中 / 中断事由あり /
            `expected_head` 不一致なら 409
    """
    runner = _runner(request)
    if not runner.enabled:
        raise HTTPException(
            status_code=403, detail="このホストでは WebUI からの更新が無効です。"
        )
    run_id, reason = runner.start(expected_head=payload.expected_head)
    if reason is not None:
        raise HTTPException(status_code=409, detail=reason)
    return UpdateRunResponse(run_id=run_id or "", run=run_payload(runner.status()))


@router.get("/update", response_class=HTMLResponse)
def update_page(request: Request) -> HTMLResponse:
    """Frontend 自身の更新ページ（マシン非依存）.

    中身は `GET /api/self-update` を読んだ `update.js` が描く（backend 側のページと
    同じ 1 本の JS を共有するため、SSR で値を焼き込まない）。
    """
    return pages.render_standalone(
        request,
        "update.html",
        {"title": "UI サーバーの更新"},
        # マシン切替の遷移先。`update` はタブではないので `/m/<id>/update` は 404 になる。
        # 機体側の同等ページは `dev/update`
        current_suffix="dev/update",
    )
