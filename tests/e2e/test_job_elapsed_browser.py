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
        # 経過表示は performance.now と setInterval で進むので、ブラウザの時計を進めて実時間の 1 秒を待たない
        page.clock.install()
        page.goto(f"{live_ui.base_url}/camera_calibration")
        acquire_control(page)
        page.get_by_test_id("job-run").click()
        expect(page.get_by_test_id("jc-status")).to_have_attribute(
            "data-status", "running"
        )
        elapsed = page.get_by_test_id("jc-elapsed")
        # 毎秒の更新は最初に実行中として描いた時点から 1 秒刻みで発火し、running の検出はそれより遅れるので、2 秒進めた後の秒数は遅れの分だけ多くなり得る
        # 秒数は決め打たず、時計を進めると表示が進むことだけを確かめる
        before = elapsed.text_content()
        assert before
        page.clock.run_for(2_000)
        expect(elapsed).not_to_have_text(before)

        response = httpx.post(f"{live_server.base_url}/api/jobs/current/abort")
        assert response.status_code == 200
        expect(page.get_by_test_id("jc-status")).to_have_attribute(
            "data-status", "aborted"
        )
        stopped = elapsed.text_content()
        assert stopped
        # 走り続けていれば必ず表示が変わる 2 秒分（毎秒の更新 2 回）を進める
        page.clock.run_for(2_000)

        expect(elapsed).to_have_text(stopped)
