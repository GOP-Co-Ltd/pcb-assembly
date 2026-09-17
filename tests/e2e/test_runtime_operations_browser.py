"""実行中の入力・操作が通信待ちの間に別ジョブへ流れない。"""

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from threading import Event

import attrs
import pytest
from playwright.sync_api import expect
from starlette.types import ASGIApp, Receive, Scope, Send

from tests.e2e.conftest import (
    E2E_MACHINE_ID,
    LiveServer,
    LiveUi,
    acquire_control,
    configure_synthetic_job,
    make_ui_settings,
    start_app,
)
from tests.helpers import FakeAudioPlayer
from web.api.app import create_app as create_api_app
from web.api.jobs.context import JobContext
from web.api.settings import Settings
from web.ui.app import create_app
from web.ui.machines import MachineEndpoint


@pytest.fixture
def live_server(e2e_settings: Settings) -> Iterator[LiveServer]:
    """実カタログのフォーム定義を、装置を使わない対話ジョブへ接続する。

    JobDefinition.run と JobManager の公開コンストラクタで構成する。
    機械フローは対象外とし、HTTP・WS・キュー・実行中パラメータは実装を通す。
    """
    app = create_api_app(e2e_settings, audio_player=FakeAudioPlayer())

    def run(ctx: JobContext) -> None:
        ctx.progress("キャリブレーションメニュー")
        while True:
            command = ctx.next_command(timeout=None)
            assert command is not None
            ctx.log(f"command:{command['type']}")
            if command["type"] == "finish":
                return

    configure_synthetic_job(app, "dispense_calibration", run)
    catalog = app.state.catalog
    catalog.register(
        attrs.evolve(catalog.get("dispense_calibration"), name="other_calibration")
    )
    server = start_app(app)
    try:
        yield LiveServer(
            base_url=f"http://127.0.0.1:{server.port}",
            settings=e2e_settings,
            port=server.port,
        )
    finally:
        server.stop()


class _DelayedOperation:
    """実 frontend の手前で、次のパラメータ要求か WS コマンドを一件保留する。"""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app
        self._kind: str | None = None
        self._received = Event()
        self._release = Event()

    def pause(self, kind: str) -> None:
        self._kind = kind

    def wait_received(self) -> bool:
        return self._received.wait(timeout=10)

    def resume(self) -> None:
        self._release.set()

    async def _wait(self) -> None:
        self._kind = None
        self._received.set()
        await asyncio.to_thread(self._release.wait, 30)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] == "PUT"
            and scope["path"].endswith("/api/jobs/current/params")
            and self._kind == "params"
        ):
            await self._wait()

        async def delayed_receive():
            message = await receive()
            if (
                message["type"] == "websocket.receive"
                and self._kind == "command"
                and json.loads(message.get("text", "{}")).get("type") == "command"
            ):
                await self._wait()
            return message

        await self._app(scope, delayed_receive, send)


@pytest.fixture
def delayed_operation_ui(
    live_server: LiveServer, tmp_path: Path
) -> Iterator[tuple[LiveUi, _DelayedOperation]]:
    endpoint = MachineEndpoint(
        machine_id=E2E_MACHINE_ID, host="127.0.0.1", port=live_server.port
    )
    transport = _DelayedOperation(
        create_app(
            make_ui_settings((endpoint,), machines_file=tmp_path / "absent.toml")
        )
    )
    server = start_app(transport)
    try:
        yield (
            LiveUi(
                origin=f"http://127.0.0.1:{server.port}", machine_ids=(E2E_MACHINE_ID,)
            ),
            transport,
        )
    finally:
        transport.resume()
        server.stop()


def _start(page, ui: LiveUi, name: str = "dispense_calibration") -> dict:
    response = page.request.post(
        f"{ui.base_url}/api/jobs/{name}", data={"params": {"line_length": 11.0}}
    )
    assert response.status == 201
    job = response.json()["job"]
    page.wait_for_function(
        "id => window.webui.jobs.currentJob()?.id === id"
        " && window.webui.jobs.currentJob()?.progress_stage === 'キャリブレーションメニュー'",
        arg=job["id"],
    )
    return job


def _restart(page, ui: LiveUi) -> dict:
    assert page.request.post(f"{ui.base_url}/api/jobs/current/abort").status == 200
    expect(page.locator("#jc-status")).to_have_attribute("data-status", "aborted")
    return _start(page, ui)


class TestScopedRuntimeOperations:
    def test_delayed_runtime_update_cannot_edit_the_replacement(
        self, delayed_operation_ui, browser_page
    ):
        ui, transport = delayed_operation_ui
        page = browser_page
        page.goto(f"{ui.base_url}/pasting/dispense_calibration")
        acquire_control(page)
        _start(page, ui)
        transport.pause("params")
        field = page.locator("#param-line_length")
        field.fill("29")
        assert transport.wait_received()
        replacement = _restart(page, ui)

        with page.expect_response("**/api/jobs/current/params") as rejected:
            transport.resume()
        assert rejected.value.status == 409
        expect(page.locator("#toasts")).to_contain_text("ジョブが切り替わりました")
        current = page.request.get(f"{ui.base_url}/api/jobs/current").json()["job"]
        assert current["id"] == replacement["id"]
        assert current["params"]["line_length"] == 11.0

        with page.expect_response("**/api/jobs/current/params") as accepted:
            field.fill("17")
        assert accepted.value.status == 200
        assert accepted.value.json()["params"] == {"line_length": 17.0}

    def test_delayed_button_command_cannot_reach_the_replacement(
        self, delayed_operation_ui, browser_page
    ):
        ui, transport = delayed_operation_ui
        page = browser_page
        page.goto(f"{ui.base_url}/pasting/dispense_calibration")
        acquire_control(page)
        _start(page, ui)
        transport.pause("command")
        page.locator("#lc-extrude").click()
        assert transport.wait_received()
        _restart(page, ui)
        transport.resume()

        expect(page.locator("#toasts")).to_contain_text("ジョブが切り替わりました")
        expect(page.locator("#jc-log")).not_to_contain_text("command:extrude")
        page.locator("#lc-suck").click()
        expect(page.locator("#jc-log")).to_contain_text("command:suck")

    def test_another_job_with_the_same_stage_does_not_enable_this_pages_buttons(
        self, live_ui: LiveUi, browser_page
    ):
        page = browser_page
        page.goto(f"{live_ui.base_url}/pasting/dispense_calibration")
        acquire_control(page)
        _start(page, live_ui, "other_calibration")

        expect(page.locator("#lc-extrude")).to_be_disabled()
        expect(page.locator("#calib-all")).to_be_disabled()
