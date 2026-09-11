"""WebUI フルスタック E2E 用の live uvicorn サーバー fixture.

実 uvicorn を 127.0.0.1 のエフェメラルポートに起動し、実 HTTP / WebSocket / MJPEG 経路を fake
カメラ + テスト用 config で検証する。サーバーの生存期間を fixture（= pytest
プロセス）内に閉じ込めるため常駐サーバーを別管理する必要がなく、 `make test-e2e` という有限コマンドの中で起動 → 検証 →
停止が完結する（常駐サーバーは Bash のタイムアウトやプロセス後始末で kill されがちで、E2E デバッグの障害になる）。

実機の `config/` を一切汚さないよう、`data/testing/config` を tmp_path に
複製して使う（[[feedback-webui-claude-self-e2e]] の方針）。

サーバーは 2 種類ある:

- `live_server` — backend WebAPI（`web.api`）。JSON API の直叩きに使う
- `live_ui` / `live_ui_two` — UI frontend（`web.ui`）。**ページ・MJPEG・WS はここを通す**

ページ取得とブラウザ操作を frontend 経由に寄せることで、リバースプロキシ経路
（`/m/{machine_id}/api/**`）が既存 E2E 全体で常時検証される。API の直叩きを
`live_server` のまま残すのは、backend 直と proxy 経由を意図的に分けて
「どちら側の回帰か」を切り分けられるようにするため。
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import attrs
import httpx
import pytest
import uvicorn
from starlette.types import ASGIApp

from tests.helpers import FakeAudioPlayer, copy_testing_config
from tests.web.api.conftest import COPPER_PCB_FIXTURE, FAKE_CAMERA_IMAGE
from web.api.app import create_app
from web.api.jobs.catalog import JobCatalog, JobDefinition
from web.api.jobs.context import JobContext, JobResult
from web.api.settings import Settings
from web.ui.app import create_app as create_ui_app
from web.ui.machines import MachineEndpoint
from web.ui.proxy import SESSION_COOKIE
from web.ui.settings import Settings as UiSettings

_STARTUP_TIMEOUT = 10.0
_HTTP_TIMEOUT = 10.0
_WS_TIMEOUT = 30.0

# 操作権の状態がブラウザへ届くまでの上限（GET /api/state 1 往復 + 描画）
_CONTROL_TIMEOUT_MS = 10_000

TERMINAL = ("succeeded", "failed", "aborted")

# backend の自己申告 machine_id（= frontend の URL prefix /m/{machine_id}）
E2E_MACHINE_ID = "e2etest"

# 実ブラウザを使う fixture。これを要求するテストへ browser マーカーを付ける
_BROWSER_FIXTURES = frozenset({"browser_page", "browser_pages"})


def _register_completion_notice_jobs(catalog: JobCatalog) -> None:
    """終了通知のブラウザE2E用 hiddenジョブを登録する。"""

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
                tab="dev",
                run=run,
                uses_machine=False,
                notify_on_completion=True,
                hidden=True,
            )
        )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Tests/e2e 配下の全テストへ自動で e2e / browser マーカーを付与する.

    個々のテストに付け忘れても `-m e2e` / `-m "not e2e"` の対象になるようにする。
    実ブラウザ（`browser_page` / `browser_pages`）を使うテストには browser も付け、
    `-m "e2e and not browser"` での切り分けを可能にする。
    """
    e2e_dir = Path(__file__).parent
    for item in items:
        if item.path.is_relative_to(e2e_dir):
            item.add_marker(pytest.mark.e2e)
            if _BROWSER_FIXTURES & set(getattr(item, "fixturenames", ())):
                item.add_marker(pytest.mark.browser)


@attrs.frozen
class LiveServer:
    """起動済み backend WebAPI のベース URL と注入 Settings."""

    base_url: str
    settings: Settings
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


def select_led_blinker(live_server: LiveServer) -> None:
    """実 fixture の led_blinker 一式を pcb root へ複製し、公開 API で選択する."""
    destination = live_server.settings.pcb_browse_root / "led_blinker"
    shutil.copytree(COPPER_PCB_FIXTURE.parent, destination)
    response = httpx.put(
        f"{live_server.base_url}/api/pcb-file",
        json={"path": "led_blinker/led_blinker.kicad_pcb"},
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
    if state == "free":
        page.locator("#control-acquire").click(timeout=timeout_ms)
    elif state == "viewer":
        page.locator("#control-takeover").click(timeout=timeout_ms)
    page.wait_for_function(
        "() => document.body.dataset.control === 'held'", timeout=timeout_ms
    )


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
    """GET /api/pasting/pad-config の現在値を返す."""
    response = httpx.get(
        f"{live_server.base_url}/api/pasting/pad-config",
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
        response = httpx.get(f"{base_url}/api/settings/machine", timeout=_HTTP_TIMEOUT)
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


def make_api_settings(root: Path, *, hostname: str) -> Settings:
    """Fake カメラ + テスト用 config + 隔離 data_dir の backend Settings を組む.

    `data/testing/config` を `root` 内に複製する。Klipper port 7126（非リッスン）なので、
    誤って実機 Moonraker に接続しない。

    Args:
        root: config / data / pcb root を置く隔離ディレクトリ（無ければ作る）
        hostname: backend の自己申告 machine_id。1 ホストに複数 backend を立てる
            `live_ui_two` で URL prefix を区別するために注入する

    Returns:
        構築済みの backend Settings
    """
    root.mkdir(parents=True, exist_ok=True)
    config_dir = copy_testing_config(root)
    pcb_root = root / "pcb"
    pcb_root.mkdir()
    data_dir = root / "data"
    data_dir.mkdir()
    return Settings(
        config_dir=config_dir,
        data_dir=data_dir,
        pcb_browse_root=pcb_root,
        # 公開範囲は既定（リポジトリ + /media + /mnt）から暗黙に広がらないため、
        # tmp の pcb root を明示的に許可する
        pcb_browse_allowed=(pcb_root,),
        pcb_browse_start=pcb_root,
        pcb_upload_dir=pcb_root / "uploads",
        mainsail_url="http://mainsail.invalid",
        hostname=hostname,
        fake_camera=True,
        fake_camera_image=FAKE_CAMERA_IMAGE,
        # 実 LAN へ mDNS を撒かない（広告 + 探索の通しは test_discovery_e2e.py が
        # ランダムなサービス型 + ループバック限定で見る）
        discovery_enabled=False,
        # 自己更新の report / ロックをリポジトリの data/ に落とさない
        update_state_dir=root / "selfupdate",
    )


def make_ui_settings(
    endpoints: tuple[MachineEndpoint, ...], *, machines_file: Path
) -> UiSettings:
    """静的登録だけを持つ frontend Settings を組む.

    Args:
        endpoints: 登録する backend（この順序が一覧とドロップダウンの順序になる）
        machines_file: 不在パスを渡す。リポジトリの `config/machines.toml` を
            テストが拾って実機の登録を混ぜないようにするため

    Returns:
        構築済みの frontend Settings
    """
    return UiSettings(
        machines=endpoints,
        machines_file=machines_file,
        discovery_enabled=False,
        # 自己更新の report / ロックをリポジトリの data/ に落とさない
        update_state_dir=machines_file.parent / "selfupdate",
    )


@pytest.fixture
def e2e_settings(tmp_path: Path) -> Settings:
    """Fake カメラ + テスト用 config + 隔離 data_dir の E2E 用 backend Settings."""
    return make_api_settings(tmp_path / "api", hostname=E2E_MACHINE_ID)


@pytest.fixture
def live_server(
    e2e_settings: Settings,
    paste_test_board_footprint_root: Path,
) -> Iterator[LiveServer]:
    """実 uvicorn の backend WebAPI を起動し、停止まで面倒を見る.

    通知音は `FakeAudioPlayer` を注入する（既定の `AlsaAudioPlayer` だとジョブの
    完了・応答待ちが実 `aplay` を起動して実スピーカーが鳴る）。
    """
    app = create_app(
        e2e_settings,
        audio_player=FakeAudioPlayer(),
        paste_test_board_footprint_root=paste_test_board_footprint_root,
    )
    _register_completion_notice_jobs(app.state.catalog)
    running = start_app(app)
    try:
        yield LiveServer(
            base_url=f"http://127.0.0.1:{running.port}",
            settings=e2e_settings,
            port=running.port,
        )
    finally:
        running.stop()


@pytest.fixture
def live_ui(live_server: LiveServer, tmp_path: Path) -> Iterator[LiveUi]:
    """`live_server` を 1 台だけ静的登録した実 frontend.

    ページ・MJPEG・WS をここへ通すことで、リバースプロキシ経路が既存 E2E 全体で 常時検証される。
    """
    endpoint = MachineEndpoint(
        machine_id=E2E_MACHINE_ID,
        host="127.0.0.1",
        port=live_server.port,
        name="E2E 機",
    )
    app = create_ui_app(
        make_ui_settings((endpoint,), machines_file=tmp_path / "absent-machines.toml")
    )
    running = start_app(app)
    try:
        yield LiveUi(
            origin=f"http://127.0.0.1:{running.port}",
            machine_ids=(E2E_MACHINE_ID,),
        )
    finally:
        running.stop()


@pytest.fixture
def live_ui_two(
    tmp_path: Path,
    paste_test_board_footprint_root: Path,
) -> Iterator[LiveUi]:
    """2 台の backend を静的登録した実 frontend（マシン切替の検証用）.

    backend を 2 つ（同じ pytest プロセス内の daemon スレッドで動く実 uvicorn）
    立て、`Settings.hostname` で machine_id を分ける
    （`socket.gethostname()` のままでは 1 ホスト上の 2 台を区別できない）。
    """
    machine_ids = ("alpha", "bravo")
    running: list[RunningServer] = []
    try:
        endpoints: list[MachineEndpoint] = []
        for machine_id in machine_ids:
            backend = start_app(
                create_app(
                    make_api_settings(tmp_path / machine_id, hostname=machine_id),
                    paste_test_board_footprint_root=paste_test_board_footprint_root,
                )
            )
            running.append(backend)
            endpoints.append(
                MachineEndpoint(
                    machine_id=machine_id,
                    host="127.0.0.1",
                    port=backend.port,
                    name=f"{machine_id} 号機",
                )
            )
        frontend = start_app(
            create_ui_app(
                make_ui_settings(
                    tuple(endpoints), machines_file=tmp_path / "absent-machines.toml"
                )
            )
        )
        running.append(frontend)
        yield LiveUi(
            origin=f"http://127.0.0.1:{frontend.port}", machine_ids=machine_ids
        )
    finally:
        # frontend から先に落とす（backend が消えた frontend への中継を作らない）
        for handle in reversed(running):
            handle.stop()


@pytest.fixture(scope="session")
def _browser():
    """Session 共有の実 Chromium（起動はセッションで 1 回）.

    pytest-playwright が未導入の環境では collection を壊さず skip する。Chromium
    はこの環境にある system binary を優先し、無ければ Playwright 既定に任せる。 テスト間の分離は
    browser_page 側の browser context で担保する。
    """
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        launch_kwargs: dict[str, Any] = {"headless": True}
        if Path("/usr/bin/chromium").exists():
            launch_kwargs["executable_path"] = "/usr/bin/chromium"
        browser = playwright.chromium.launch(**launch_kwargs)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def browser_pages(_browser):
    """独立した browser context の page を必要な数だけ作るファクトリ.

    `browser_page` の実装を集約する（現状の利用は 1 枚のみ。複数ページを開く検証は
    MR6 以降で使う想定）。作った context は teardown でまとめて閉じる。
    """
    contexts = []

    def new_page():
        context = _browser.new_context()
        contexts.append(context)
        return context.new_page()

    try:
        yield new_page
    finally:
        for context in contexts:
            context.close()


@pytest.fixture
def browser_page(browser_pages):
    """Playwright sync API の実 Chromium page（1 枚）.

    テストごとに新しい browser context（cookie / localStorage / viewport が
    独立）を作り、teardown で context ごと閉じる。
    """
    return browser_pages()
