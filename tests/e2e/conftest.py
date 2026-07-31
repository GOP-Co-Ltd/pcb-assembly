"""WebUI フルスタック E2E 用の live uvicorn サーバー fixture.

実 uvicorn を 127.0.0.1 のエフェメラルポートに起動し、実 HTTP / WebSocket / MJPEG 経路を fake
カメラ + テスト用 config で検証する。サーバーの生存期間を fixture（= pytest
プロセス）内に閉じ込めるため常駐サーバーを別管理する必要がなく、 `make test-e2e` という有限コマンドの中で起動 → 検証 →
停止が完結する（常駐サーバーは Bash のタイムアウトやプロセス後始末で kill されがちで、E2E デバッグの障害になる）。

実機の `config/` を一切汚さないよう、`data/testing/config` を tmp_path に
複製して使う（[[feedback-webui-claude-self-e2e]] の方針）。
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

from tests.helpers import copy_testing_config
from tests.web.api.conftest import COPPER_PCB_FIXTURE, FAKE_CAMERA_IMAGE
from web.api.app import create_app
from web.api.jobs.catalog import JobCatalog, JobDefinition
from web.api.jobs.context import JobContext, JobResult
from web.api.settings import Settings

_STARTUP_TIMEOUT = 10.0
_HTTP_TIMEOUT = 10.0
_WS_TIMEOUT = 30.0

TERMINAL = ("succeeded", "failed", "aborted")


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
    実ブラウザ（browser_page）を使うテストには browser も付け、 `-m "e2e and not browser"`
    での切り分けを可能にする。
    """
    e2e_dir = Path(__file__).parent
    for item in items:
        if item.path.is_relative_to(e2e_dir):
            item.add_marker(pytest.mark.e2e)
            if "browser_page" in getattr(item, "fixturenames", ()):
                item.add_marker(pytest.mark.browser)


@attrs.frozen
class LiveServer:
    """起動済み実サーバーのベース URL と注入 Settings."""

    base_url: str
    settings: Settings

    @property
    def ws_url(self) -> str:
        """WebSocket 用ベース URL（http -> ws）."""
        return "ws://" + self.base_url.removeprefix("http://")


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


@pytest.fixture
def e2e_settings(tmp_path: Path) -> Settings:
    """Fake カメラ + テスト用 config + 隔離 data_dir の E2E 用 Settings.

    `data/testing/config` を tmp_path 内に複製する。Klipper port 7126（非リッスン）なので、
    誤って実機 Moonraker に接続しない。
    """
    config_dir = copy_testing_config(tmp_path)
    pcb_root = tmp_path / "pcb"
    pcb_root.mkdir()
    data_dir = tmp_path / "data"
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
        fake_camera=True,
        fake_camera_image=FAKE_CAMERA_IMAGE,
    )


@pytest.fixture
def live_server(e2e_settings: Settings) -> Iterator[LiveServer]:
    """実 uvicorn を daemon スレッドで起動し、停止まで面倒を見る.

    port=0 でエフェメラルポートを OS に割り当てさせ、起動後に実ポートを取得する。
    """
    app = create_app(e2e_settings)
    _register_completion_notice_jobs(app.state.catalog)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + _STARTUP_TIMEOUT
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn が時間内に起動しなかった")
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]

    try:
        yield LiveServer(base_url=f"http://127.0.0.1:{port}", settings=e2e_settings)
    finally:
        server.should_exit = True
        thread.join(timeout=_STARTUP_TIMEOUT)


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
def browser_page(_browser):
    """Playwright sync API の実 Chromium page.

    テストごとに新しい browser context（cookie / localStorage / viewport が
    独立）を作り、teardown で context ごと閉じる。
    """
    context = _browser.new_context()
    try:
        yield context.new_page()
    finally:
        context.close()
