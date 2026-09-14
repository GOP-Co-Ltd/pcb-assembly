"""Frontend 自身のソフトウェア更新（`/update` ページとその API）.

frontend 専用機（`pcbasm-ui.service` だけが動き backend が居ないホスト）には
backend のジョブ基盤も操作権も無いので、frontend が自前の更新 API を持つ。
`machines_api.py` が先例。

パスは backend と**意図的に変える**（`/m/{id}/api/update/**` は proxy で backend 行き
なので、frontend 自身の入口は `/api/self-update/**`）。`pages.router` の `/{tab}`
キャッチオールに食われないよう、アプリでは pages より**先に**登録する。

全ページのトップバーが読む `GET /api/update-notice` もここに置く。frontend 自身と
表示中の機体 backend の両方を見て、**バッジ 1 個分の表示値に畳んでから**返す
（どちらに何件あるかの判断を JS に持たせない）。

frontend には操作権（control lease）が無い。多層の安全弁で守るが、**いずれも認証では
ない**（LAN に居る者は誰でも押せる）:

1. `expected_head` 必須の 2 段階（楽観ロック）
2. 単一実行ロック（flock。同居機の backend とも共有する）
3. `enabled` 設定（env で無効化。無効時は 403 + ボタン非表示）
4. ブラウザの `confirm()` にホスト名と再起動する unit を出す
"""

from __future__ import annotations

import socket
from collections.abc import Sequence

import attrs
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from web.api.models import UpdateRunResponse, UpdateStatusResponse
from web.selfupdate.report import UpdatePlan, run_payload, status_payload
from web.selfupdate.runner import UpdateRunner
from web.ui import pages
from web.ui.machine_client import BackendGateway, BackendUnavailable, MachineClient
from web.ui.machines import MachineRegistry, UnknownMachine
from web.ui.models import UPDATE_INDEX_PATH, UpdateNoticeResponse

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


@attrs.frozen
class NoticeSource:
    """通知が見る 1 ホスト分の観測値（表示文字列はそのホストのサーバが組んだもの）."""

    hostname: str
    summary: str
    href: str
    available: bool


def compose_notice(sources: Sequence[NoticeSource]) -> UpdateNoticeResponse:
    """待っている更新をバッジ 1 個分の表示値にまとめる（純関数）.

    遷移先は「1 ホストだけならそのホストの更新ページ、複数なら UI サーバーの更新
    ページ」。後者は各機体のページへのリンクを持つので、そこから辿れる。

    同居機では frontend と backend が同じホスト名・同じ作業リポジトリを見るので、
    ホスト名で畳む（畳まないと 1 件の更新が「2 件」と出る）。

    Args:
        sources: 通知の対象（UI サーバー自身と、表示中の機体）

    Returns:
        バッジの表示値（待っている更新が無ければ `available=False`）
    """
    pending: list[NoticeSource] = []
    seen: set[str] = set()
    for source in sources:
        if not source.available or source.hostname in seen:
            continue
        seen.add(source.hostname)
        pending.append(source)
    if not pending:
        return UpdateNoticeResponse()
    detail = "\n".join(f"{source.hostname}: {source.summary}" for source in pending)
    if len(pending) == 1:
        return UpdateNoticeResponse(
            available=True,
            label=f"ソフトウェア更新あり（{pending[0].hostname}）",
            detail=detail,
            href=pending[0].href,
        )
    return UpdateNoticeResponse(
        available=True,
        label=f"ソフトウェア更新あり（{len(pending)} 件）",
        detail=detail,
        href=UPDATE_INDEX_PATH,
    )


def _self_source(runner: UpdateRunner) -> NoticeSource:
    """UI サーバー自身の観測値（fetch はしない。定期 fetch が別に回っている）."""
    status = _status(runner, runner.plan())
    return NoticeSource(
        hostname=status.hostname,
        summary=status.summary,
        href=UPDATE_INDEX_PATH,
        available=status.update_available,
    )


async def _machine_source(request: Request, machine_id: str) -> NoticeSource | None:
    """表示中の機体 backend の観測値（読めなければ None）.

    通知は補助情報なので、**backend に到達できなくてもエラーにしない**。ここで
    例外を通すと、機体が落ちている間じゅうトップバーがエラーを出し続ける。
    """
    registry: MachineRegistry = request.app.state.registry
    gateway: BackendGateway = request.app.state.gateway
    try:
        endpoint = registry.resolve(machine_id)
        status = await MachineClient(endpoint, gateway).update_status()
    except (UnknownMachine, BackendUnavailable):
        return None
    return NoticeSource(
        hostname=status.hostname,
        summary=status.summary,
        href=f"/m/{endpoint.machine_id}/dev/update",
        available=status.update_available,
    )


@router.get("/api/update-notice")
async def get_update_notice(
    request: Request, machine_id: str | None = None
) -> UpdateNoticeResponse:
    """トップバーの更新通知（UI サーバー自身と、表示中の機体を見る）.

    Args:
        request: ランナー・レジストリ・gateway を持つアプリへの参照
        machine_id: 表示中の機体（マシン非依存のページでは None）

    Returns:
        バッジの表示値（待っている更新が無ければ `available=False`）
    """
    sources = [_self_source(_runner(request))]
    machine = None if machine_id is None else await _machine_source(request, machine_id)
    if machine is not None:
        sources.append(machine)
    return compose_notice(sources)


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
