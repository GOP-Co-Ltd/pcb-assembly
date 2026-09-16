"""質量の変更後に、古い算出値や遅延応答を適用しない。"""

import asyncio
from collections.abc import Iterator
from pathlib import Path
from threading import Event
from urllib.parse import parse_qs

import pytest
from playwright.sync_api import expect
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from tests.e2e.conftest import (
    E2E_MACHINE_ID,
    LiveServer,
    LiveUi,
    acquire_control,
    make_ui_settings,
    start_app,
    wait_machine_field,
)
from web.ui.app import create_app
from web.ui.machines import MachineEndpoint


class _DelayedCalibration:
    """実 frontend の手前で、質量 20 の HTTP 要求だけを保留する。"""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app
        self._received = Event()
        self._release = Event()
        self._error = Event()

    def wait_received(self) -> bool:
        return self._received.wait(timeout=10)

    def resume(self, *, error: bool = False) -> None:
        if error:
            self._error.set()
        self._release.set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["path"].endswith("/api/pasting/loading/calibration")
            and parse_qs(scope["query_string"].decode()).get("mass_mg") == ["20"]
        ):
            self._received.set()
            await asyncio.to_thread(self._release.wait, 10)
            if self._error.is_set():
                await JSONResponse(
                    {"detail": "計算サービスが一時的に利用できません"}, status_code=503
                )(scope, receive, send)
                return
        await self._app(scope, receive, send)


@pytest.fixture
def delayed_calibration_ui(
    live_server: LiveServer, tmp_path: Path
) -> Iterator[tuple[LiveUi, _DelayedCalibration]]:
    endpoint = MachineEndpoint(
        machine_id=E2E_MACHINE_ID, host="127.0.0.1", port=live_server.port
    )
    transport = _DelayedCalibration(
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


def _open_calibration(ui: LiveUi, page) -> None:
    page.goto(f"{ui.base_url}/pasting/loading")
    acquire_control(page)
    page.locator("#lc-rotations").fill("5")
    page.locator("#lc-mass-mg").fill("10")
    expect(page.locator("#lc-rotations-per-ul")).to_have_text("1.890000")


class TestCurrentLoadingCalibration:
    @pytest.mark.parametrize("older_error", [False, True])
    def test_input_invalidates_results_and_an_older_response_cannot_restore_them(
        self,
        live_server: LiveServer,
        delayed_calibration_ui,
        browser_page,
        older_error: bool,
    ):
        ui, transport = delayed_calibration_ui
        _open_calibration(ui, browser_page)

        # debounce の待機中から全適用ボタンを塞ぐ。同一 input イベント内で観測する。
        assert browser_page.locator("#lc-mass-mg").evaluate("""input => {
            input.value = '20';
            input.dispatchEvent(new Event('input', {bubbles: true}));
            return [...document.querySelectorAll('[id^="lc-apply-"]')]
                .every(button => button.disabled);
        }""")
        assert transport.wait_received()
        expect(browser_page.locator("#lc-rotations-per-ul")).to_have_text("-")
        browser_page.locator("#lc-mass-mg").fill("40")
        output = browser_page.locator("#lc-rotations-per-ul")
        expect(output).to_have_text("0.472500")

        with browser_page.expect_request_finished(
            lambda request: "/loading/calibration?" in request.url
            and "mass_mg=20&" in request.url
        ):
            transport.resume(error=older_error)
        # 完了した古い HTTP 応答をブラウザが描画へ反映する機会を経てから比較する。
        browser_page.evaluate(
            "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
        )
        expect(output).to_have_text("0.472500")
        expect(browser_page.locator("#lc-calibration-message")).to_have_text("")
        browser_page.locator("#lc-apply-rotations-per-ul").click()
        wait_machine_field(
            live_server.base_url, "paste_dispenser.rotations_per_ul", 0.4725
        )

    def test_a_failed_current_request_keeps_results_unavailable_until_retry(
        self, live_server: LiveServer, delayed_calibration_ui, browser_page
    ):
        ui, transport = delayed_calibration_ui
        _open_calibration(ui, browser_page)
        transport.resume(error=True)
        browser_page.locator("#lc-mass-mg").fill("20")
        expect(browser_page.locator("#lc-calibration-message")).to_contain_text(
            "計算サービスが一時的に利用できません"
        )
        for output in (
            "volume-ul",
            "rotations-per-ul",
            "dispense-rate",
            "dispense-accel",
        ):
            expect(browser_page.locator(f"#lc-{output}")).to_have_text("-")
        for action in ("rotations-per-ul", "dispense-rate", "dispense-accel", "all"):
            expect(browser_page.locator(f"#lc-apply-{action}")).to_be_disabled()

        browser_page.locator("#lc-mass-mg").fill("40")
        expect(browser_page.locator("#lc-rotations-per-ul")).to_have_text("0.472500")
        expect(browser_page.locator("#lc-calibration-message")).to_have_text("")
        browser_page.locator("#lc-apply-rotations-per-ul").click()
        wait_machine_field(
            live_server.base_url, "paste_dispenser.rotations_per_ul", 0.4725
        )
