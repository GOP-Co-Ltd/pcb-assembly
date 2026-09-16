"""確認ダイアログの空欄検証・中止・応答拒否からの回復。"""

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from threading import Event

import pytest
from playwright.sync_api import expect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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
from web.api.jobs.context import JobContext, JobResult, PromptSpec
from web.api.settings import Settings
from web.ui.app import create_app
from web.ui.machines import MachineEndpoint


@pytest.fixture
def live_server(e2e_settings: Settings) -> Iterator[LiveServer]:
    app = create_api_app(e2e_settings, audio_player=FakeAudioPlayer())

    def run(ctx: JobContext) -> JobResult:
        answer = ctx.prompt(
            PromptSpec(
                kind="number",
                message="実測値を入力してください",
                default=12.0,
                false_label="中止",
            )
        )
        return JobResult(summary=f"accepted:{answer!r}")

    configure_synthetic_job(app, "camera_calibration", run)
    server = start_app(app)
    try:
        yield LiveServer(
            base_url=f"http://127.0.0.1:{server.port}",
            settings=e2e_settings,
            port=server.port,
        )
    finally:
        server.stop()


class _DelayedPromptTransport:
    """最初の回答または状態スナップショットを実 frontend の手前で保留する。"""

    def __init__(self, app: ASGIApp, *, snapshot: bool = False) -> None:
        self._app = app
        self._snapshot = snapshot
        self._received = Event()
        self._release = Event()

    def wait_received(self) -> bool:
        return self._received.wait(timeout=10)

    def resume(self) -> None:
        self._release.set()

    async def _wait(self) -> None:
        self._received.set()
        await asyncio.to_thread(self._release.wait, 30)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            self._snapshot
            and scope["type"] == "http"
            and scope["method"] == "GET"
            and scope["path"].endswith("/api/jobs/current")
            and not self._received.is_set()
        ):

            async def delayed_send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    await self._wait()
                await send(message)

            await self._app(scope, receive, delayed_send)
            return

        async def delayed_receive():
            message = await receive()
            if (
                message["type"] == "websocket.receive"
                and not self._snapshot
                and not self._received.is_set()
                and json.loads(message.get("text", "{}")).get("type")
                == "respond_prompt"
            ):
                await self._wait()
            return message

        await self._app(scope, delayed_receive, send)


@pytest.fixture
def delayed_prompt_ui(
    live_server: LiveServer, tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[tuple[LiveUi, _DelayedPromptTransport]]:
    endpoint = MachineEndpoint(
        machine_id=E2E_MACHINE_ID, host="127.0.0.1", port=live_server.port
    )
    transport = _DelayedPromptTransport(
        create_app(
            make_ui_settings((endpoint,), machines_file=tmp_path / "absent.toml")
        ),
        snapshot=getattr(request, "param", False),
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


def _open_prompt(page, ui: LiveUi) -> None:
    page.goto(f"{ui.base_url}/posctrl/camera_calibration")
    acquire_control(page)
    page.locator("#job-run").click()
    expect(page.locator("#jc-prompt-input")).to_have_value("12")


class TestJobPrompts:
    @pytest.mark.parametrize("delayed_prompt_ui", [True], indirect=True)
    def test_initial_snapshot_cannot_hide_a_prompt_started_while_it_was_delayed(
        self, delayed_prompt_ui, browser_page
    ):
        ui, transport = delayed_prompt_ui
        page = browser_page
        page.goto(
            f"{ui.base_url}/posctrl/camera_calibration", wait_until="domcontentloaded"
        )
        assert transport.wait_received()
        acquire_control(page)
        page.locator("#job-run").click()
        field = page.locator("#jc-prompt-input")
        expect(field).to_have_value("12")
        field.fill("23")

        with page.expect_response("**/api/jobs/current"):
            transport.resume()
        page.evaluate(
            "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
        )
        expect(page.locator("#jc-status")).to_have_attribute(
            "data-status", "waiting_input"
        )
        expect(page.locator("#jc-prompt")).to_be_visible()
        expect(field).to_have_value("23")
        page.locator("#jc-prompt-ok").click()
        expect(page.locator("#jc-summary")).to_have_text("accepted:23.0")

    def test_empty_number_stays_pending_and_explicit_zero_can_be_submitted(
        self, live_ui: LiveUi, browser_page
    ):
        page = browser_page
        _open_prompt(page, live_ui)
        field = page.locator("#jc-prompt-input")
        field.fill("")
        page.locator("#jc-prompt-ok").click()

        expect(page.locator("#jc-prompt")).to_be_visible()
        expect(page.locator("#jc-status")).to_have_attribute(
            "data-status", "waiting_input"
        )
        assert field.evaluate("input => input.validity.valueMissing")
        expect(field).to_have_accessible_name("実測値を入力してください")
        field.fill("0")
        field.press("Enter")
        expect(page.locator("#jc-summary")).to_have_text("accepted:0.0")
        expect(page.locator("#jc-prompt")).to_be_hidden()

    def test_cancellation_does_not_require_a_number(
        self, live_ui: LiveUi, browser_page
    ):
        page = browser_page
        _open_prompt(page, live_ui)
        page.locator("#jc-prompt-input").fill("")
        page.locator("#jc-prompt-no").click()

        expect(page.locator("#jc-summary")).to_have_text("accepted:False")
        expect(page.locator("#jc-prompt")).to_be_hidden()

    def test_rejected_answer_keeps_the_prompt_visible_for_both_operators(
        self, delayed_prompt_ui, browser_pages
    ):
        ui, transport = delayed_prompt_ui
        operator = browser_pages()
        _open_prompt(operator, ui)
        operator.locator("#jc-prompt-input").fill("23")
        operator.locator("#jc-prompt-ok").click()
        assert transport.wait_received()

        replacement = browser_pages()
        replacement.goto(f"{ui.base_url}/posctrl/camera_calibration")
        acquire_control(replacement)
        expect(operator.locator("body")).to_have_attribute("data-control", "viewer")
        transport.resume()
        expect(operator.locator("#toasts")).to_contain_text("操作権は")
        expect(operator.locator("#jc-prompt")).to_be_visible()
        expect(operator.locator("#jc-prompt-input")).to_have_value("23")
        expect(replacement.locator("#jc-prompt")).to_be_visible()

        replacement.locator("#jc-prompt-input").fill("31")
        replacement.locator("#jc-prompt-ok").click()
        for page in (operator, replacement):
            expect(page.locator("#jc-summary")).to_have_text("accepted:31.0")
            expect(page.locator("#jc-prompt")).to_be_hidden()
