"""実 uvicorn で起動した backend と frontend を、HTTP・WebSocket・実ブラウザから操作する部品."""

from __future__ import annotations

import json
import shutil
import threading
import time
from collections.abc import Callable
from typing import Any

import attrs
import httpx
import uvicorn
from fastapi import FastAPI
from starlette.types import ASGIApp

from tests.support.paths import LED_BLINKER_PCB
from tests.support.web import job_manager
from web.api.backend import Backend
from web.api.env import Env
from web.api.jobs.runtime.catalog import JobCatalog, JobDefinition
from web.api.jobs.runtime.context import JobContext
from web.api.jobs.runtime.record import JobResult
from web.contract.identity import SESSION_COOKIE

_STARTUP_TIMEOUT = 10.0
_HTTP_TIMEOUT = 10.0
_WS_TIMEOUT = 30.0

# 操作権の状態がブラウザへ届くまでの上限（GET /api/session/state 1 往復 + 描画）
_CONTROL_TIMEOUT_MS = 10_000

TERMINAL = ("succeeded", "failed", "aborted")

# backend の自己申告 machine_id（= frontend の URL prefix /m/{machine_id}）
E2E_MACHINE_ID = "e2etest"


def configure_synthetic_job(
    app: FastAPI, name: str, run: Callable[[JobContext], JobResult | None]
) -> None:
    """起動前の app に、既存フォーム定義を使う装置非使用の合成ジョブを構成する。

    JobDefinition / JobManager の公開 API を使い、HTTP・WS・キューは実装を通す。
    機械フロー自体を検証するテストには使わない。
    """
    backend: Backend = app.state.backend
    synthetic = attrs.evolve(backend.catalog.get(name), run=run, uses_machine=False)
    catalog = JobCatalog()
    for definition in backend.catalog.list():
        if definition.name == name:
            definition = synthetic
        catalog.register(definition)
    app.state.backend = attrs.evolve(
        backend, catalog=catalog, jobs=job_manager(backend, catalog)
    )


def register_completion_notice_jobs(catalog: JobCatalog) -> None:
    """終了通知のブラウザE2E用ジョブを登録する。"""

    def succeed(ctx: JobContext) -> JobResult:
        return JobResult(summary="通知テスト完了")

    def fail(ctx: JobContext) -> None:
        raise RuntimeError("通知テスト失敗")

    def wait_for_abort(ctx: JobContext) -> None:
        ctx.next_command(timeout=None)

    for name, label, run in (
        ("completion_notice_success", "通知テスト成功", succeed),
        ("completion_notice_failure", "通知テスト失敗", fail),
        ("completion_notice_abort", "通知テスト中止", wait_for_abort),
    ):
        catalog.register(
            JobDefinition(
                name=name,
                label=label,
                run=run,
                uses_machine=False,
                notify_on_completion=True,
            )
        )


@attrs.frozen
class LiveServer:
    """起動済み backend WebAPI のベース URL と注入 Env."""

    base_url: str
    settings: Env
    # frontend の静的登録に渡す実ポート（エフェメラル）
    port: int

    @property
    def ws_url(self) -> str:
        """WebSocket 用ベース URL（http -> ws）."""
        return "ws://" + self.base_url.removeprefix("http://")


@attrs.frozen
class LiveUi:
    """起動済み UI frontend の URL 群と登録済み machine_id.

    `base_url` は ``/m/{machine_id}`` を含む（ページ取得も API も同じ prefix に
    乗るので、テスト側で prefix を組み立てずに済む）。未 prefix の URL や
    ``/static`` を触るテストは `origin` を使う。
    """

    origin: str
    machine_ids: tuple[str, ...]

    @property
    def machine_id(self) -> str:
        """既定で操作するマシン（登録順の先頭）."""
        return self.machine_ids[0]

    @property
    def base_url(self) -> str:
        """既定マシンの prefix 付きベース URL."""
        return f"{self.origin}/m/{self.machine_id}"

    @property
    def ws_origin(self) -> str:
        """WebSocket 用の origin（http -> ws）."""
        return "ws://" + self.origin.removeprefix("http://")

    @property
    def ws_url(self) -> str:
        """既定マシンの prefix 付き WebSocket ベース URL."""
        return f"{self.ws_origin}/m/{self.machine_id}"


@attrs.frozen
class RunningServer:
    """Daemon スレッドで動いている uvicorn とその実ポート.

    停止を明示的に呼べるようにしてあるのは、「backend を落とすと frontend 経由の WS が
    閉じる」ような**サーバーを途中で殺す**検証を書けるようにするため。
    """

    port: int
    server: uvicorn.Server
    thread: threading.Thread

    def stop(self) -> None:
        """停止を要求してスレッドの終了を待つ（冪等）."""
        self.server.should_exit = True
        self.thread.join(timeout=_STARTUP_TIMEOUT)


def start_app(app: ASGIApp) -> RunningServer:
    """ASGI アプリを実 uvicorn（127.0.0.1・エフェメラルポート）で起動する.

    port=0 でポートを OS に割り当てさせ、起動後に実ポートを取得する。呼び出し側は
    必ず `RunningServer.stop` を呼ぶ（fixture の finally / テストの try-finally）。

    Args:
        app: 起動する ASGI アプリ

    Returns:
        実ポートと停止手段を持つハンドル

    Raises:
        RuntimeError: 起動が `_STARTUP_TIMEOUT` 以内に完了しない場合
    """
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + _STARTUP_TIMEOUT
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn が時間内に起動しなかった")
        time.sleep(0.05)
    return RunningServer(
        port=server.servers[0].sockets[0].getsockname()[1],
        server=server,
        thread=thread,
    )


# select_led_blinker が選ぶ PCB（pcb root からの相対パス。pad-config の PATCH に expected_pcb として添える）
SELECTED_LED_BLINKER = "led_blinker/led_blinker.kicad_pcb"


def select_led_blinker(live_server: LiveServer) -> None:
    """実 fixture の led_blinker 一式を pcb root へ複製し、公開 API で選択する."""
    destination = live_server.settings.pcb_browse_root / "led_blinker"
    shutil.copytree(LED_BLINKER_PCB.parent, destination)
    response = httpx.put(
        f"{live_server.base_url}/api/selection/pcb-file",
        json={"path": SELECTED_LED_BLINKER},
        timeout=_HTTP_TIMEOUT,
    )
    assert response.status_code == 200, response.text


def acquire_control(page: Any, *, timeout_ms: float = _CONTROL_TIMEOUT_MS) -> None:
    """開いているページで操作権を確保し、`data-control` が held になるまで待つ.

    UI は fail-closed で、ページを開いた直後は `body[data-control] = "viewer"`、
    `data-requires-control` の要素には `inert` が付いている（クリックが届かない）。
    ブラウザから変更操作をする検証は先に操作権を取る必要がある。

    空いていれば取得、他クライアントが保持していれば奪取する。テストの下準備は
    backend を直叩きするので（`select_led_blinker` 等はゲート付きエンドポイントを
    叩き、セッションヘッダが無いため `anonymous` がリースを握る）、ブラウザ側は
    閲覧者から始まるのが普通。

    SSR の初期値も `viewer` で「サーバがそう言っている」のと区別できないため、
    先に `control.refresh()` でサーバの事実を取り込んでから押すボタンを決める。
    """
    page.wait_for_function(
        "() => window.webui?.control !== undefined", timeout=timeout_ms
    )
    state = page.evaluate(
        """async () => {
            await window.webui.control.refresh();
            return document.body.dataset.control;
        }"""
    )
    assert state in ("free", "viewer", "held"), f"操作権の状態が読めない: {state}"
    menu = page.get_by_test_id("topbar-menu")
    open_menu = state != "held" and menu.get_attribute("open") is None
    if open_menu:
        menu.locator("summary").click(timeout=timeout_ms)
    if state == "free":
        page.get_by_test_id("control-acquire").click(timeout=timeout_ms)
    elif state == "viewer":
        page.get_by_test_id("control-takeover").click(timeout=timeout_ms)
    page.wait_for_function(
        "() => document.body.dataset.control === 'held'", timeout=timeout_ms
    )
    if open_menu:
        menu.locator("summary").click(timeout=timeout_ms)


def session_headers(page: Any) -> dict[str, str]:
    """ブラウザのセッションを backend 直叩き用のヘッダにする（同一クライアント扱い）.

    `pcbasm_session` は httpOnly なのでページの JS からは読めない。テストが backend を
    直に叩くとき（ジョブ開始など）に、ブラウザと同じ操作権で通す必要があるので
    browser context から取り出してヘッダへ載せる（frontend 経由と違い backend 直叩き
    では自称ヘッダがそのまま採用される）。
    """
    for cookie in page.context.cookies():
        if cookie["name"] == SESSION_COOKIE:
            return {"X-Pcbasm-Session": cookie["value"]}
    raise AssertionError(f"{SESSION_COOKIE} cookie がまだ発行されていない")


def get_pad_config(live_server: LiveServer) -> dict[str, Any]:
    """GET /api/pasting/board の現在値を返す."""
    response = httpx.get(
        f"{live_server.base_url}/api/pasting/board",
        timeout=_HTTP_TIMEOUT,
    )
    assert response.status_code == 200, response.text
    return response.json()


def wait_for_config(
    live_server: LiveServer,
    predicate: Callable[[dict[str, Any]], bool],
    describe: str,
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Pad-config が predicate を満たすまで GET でポーリングし、満たした config を返す."""
    deadline = time.monotonic() + timeout
    while True:
        config = get_pad_config(live_server)
        if predicate(config):
            return config
        if time.monotonic() > deadline:
            raise AssertionError(f"pad-config が期待状態にならない: {describe}")
        time.sleep(0.05)


def wait_machine_field(
    base_url: str, key: str, expected: object, *, timeout: float = 5.0
) -> None:
    """Machine 設定の 1 フィールドが期待値になるまで REST 経由で待つ."""
    deadline = time.monotonic() + timeout
    while True:
        response = httpx.get(f"{base_url}/api/machine_config", timeout=_HTTP_TIMEOUT)
        fields = {field["key"]: field for field in response.json()["fields"]}
        if fields[key]["value"] == expected:
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"{key} が {expected} に保存されない: {fields[key]}")
        time.sleep(0.05)


def respond_prompt(
    ws: Any, prompt: dict[str, Any], answered: set[str], number_answer: float
) -> None:
    """Prompt（または job_status.pending_prompt）へ kind に応じて 1 度だけ応答する."""
    prompt_id = prompt["id"]
    if prompt_id in answered:
        return
    answer: bool | float = True if prompt["kind"] == "confirm" else number_answer
    ws.send(
        json.dumps({"type": "respond_prompt", "prompt_id": prompt_id, "answer": answer})
    )
    answered.add(prompt_id)


def drive_job_demo(ws: Any, *, number_answer: float) -> tuple[dict[str, Any], set[str]]:
    """WS イベントを受信駆動で処理し、終端 job_status とその間に観測した type 集合を返す.

    prompt は confirm=True / number=number_answer で応答する。job_status の
    pending_prompt 経由でも応答できるよう二重化し、prompt_id で重複応答を防ぐ。
    """
    answered: set[str] = set()
    seen_types: set[str] = set()
    while True:
        event = json.loads(ws.recv(timeout=_WS_TIMEOUT))
        seen_types.add(event["type"])
        if event["type"] == "prompt":
            respond_prompt(ws, event["prompt"], answered, number_answer)
        elif event["type"] == "job_status":
            job = event["job"]
            pending = job.get("pending_prompt")
            if pending is not None:
                respond_prompt(ws, pending, answered, number_answer)
            if job["status"] in TERMINAL:
                return job, seen_types


def drive_choice_job(ws: Any, *, answer: str) -> dict[str, Any]:
    """Choice prompt へ 1 度だけ応答し、終端 job_status を返す.

    `drive_job_demo` は confirm / number 用なので、選択肢を返す prompt には使えない。
    """
    answered: set[str] = set()

    def respond(prompt: dict[str, Any]) -> None:
        if prompt["id"] in answered:
            return
        ws.send(
            json.dumps(
                {"type": "respond_prompt", "prompt_id": prompt["id"], "answer": answer}
            )
        )
        answered.add(prompt["id"])

    while True:
        event = json.loads(ws.recv(timeout=_WS_TIMEOUT))
        if event["type"] == "prompt":
            respond(event["prompt"])
        elif event["type"] == "job_status":
            job = event["job"]
            pending = job.get("pending_prompt")
            if pending is not None:
                respond(pending)
            if job["status"] in TERMINAL:
                return job


def wait_first_prompt(ws: Any) -> dict[str, Any]:
    """最初の prompt イベントを受信して返す（WAITING_INPUT で停止した証跡）.

    job_status の pending_prompt 経由でも捕捉できるよう二重化する。
    """
    while True:
        event = json.loads(ws.recv(timeout=_WS_TIMEOUT))
        if event["type"] == "prompt":
            return event["prompt"]
        if event["type"] == "job_status":
            pending = event["job"].get("pending_prompt")
            if pending is not None:
                return pending


def pad_row(page: Any, node_id: str) -> Any:
    """Pad 編集の表で ``node_id`` の行."""
    return page.locator(f'[data-testid="pad-tree-row"][data-node-id="{node_id}"]')


def pad_child_rows(page: Any) -> Any:
    """Pad 編集の表で、ルート（L0）以外の行."""
    return page.locator('[data-testid="pad-tree-row"]:not([data-node-id="L0"])')


def pad_field(row: Any, testid: str, field: str) -> Any:
    """Pad 編集の行で ``field`` の欄にある ``testid`` の要素（入力・選択・override の印など）."""
    return row.locator(f'[data-testid="{testid}"][data-field="{field}"]')
