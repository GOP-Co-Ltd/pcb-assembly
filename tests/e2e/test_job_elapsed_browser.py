"""ジョブコンソールの経過時間（実測のタクトタイム）を実ブラウザで確かめる E2E."""

from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import expect

from pcbasm.hal.fake import FakeAudioPlayer
from tests.support.browser import (
    LiveServer,
    LiveUi,
    acquire_control,
    configure_synthetic_job,
    start_app,
)
from tests.support.web import devices_with_audio
from web.api.app import create_app
from web.api.env import Env
from web.api.jobs.runtime.context import JobContext


@pytest.fixture
def live_server(e2e_settings: Env) -> Iterator[LiveServer]:
    """中止されるまで走り続けるジョブを、カメラ校正のページから始められる backend."""
    app = create_app(
        e2e_settings, devices=devices_with_audio(e2e_settings, FakeAudioPlayer())
    )

    def wait_for_abort(ctx: JobContext) -> None:
        ctx.next_command(timeout=None)

    configure_synthetic_job(app, "camera_calibration", wait_for_abort)
    server = start_app(app)
    try:
        yield LiveServer(
            base_url=f"http://127.0.0.1:{server.port}",
            settings=e2e_settings,
            port=server.port,
        )
    finally:
        server.stop()


class TestElapsed:
    """実行中は毎秒進み、終端で止まった値を残す."""

    def test_ticks_while_running_and_stops_at_terminal(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        page = browser_page
        page.goto(f"{live_ui.base_url}/camera_calibration")
        acquire_control(page)
        page.get_by_test_id("job-run").click()
        elapsed = page.get_by_test_id("jc-elapsed")
        expect(elapsed).to_have_text("経過 0時間0分1秒", timeout=10_000)

        response = httpx.post(f"{live_server.base_url}/api/jobs/current/abort")
        assert response.status_code == 200
        expect(page.get_by_test_id("jc-status")).to_have_attribute(
            "data-status", "aborted"
        )
        stopped = elapsed.text_content()
        assert stopped
        page.wait_for_timeout(1_500)

        expect(elapsed).to_have_text(stopped)
