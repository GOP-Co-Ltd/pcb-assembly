"""校正結果の反映・破棄と、次のジョブ開始が重なる場合の実ブラウザ検証。"""

from collections.abc import Iterator
from pathlib import Path

import attrs
import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import (
    E2E_MACHINE_ID,
    LiveServer,
    LiveUi,
    acquire_control,
    make_api_settings,
)
from tests.e2e.network import DelayedHttp, delayed_ui
from tests.web.api.conftest import CHECKERBOARD_CAMERA_IMAGE
from web.api.config_store import ConfigStore
from web.api.settings import Settings


@pytest.fixture
def e2e_settings(tmp_path: Path) -> Settings:
    settings = make_api_settings(tmp_path / "api", hostname=E2E_MACHINE_ID)
    ConfigStore(settings.config_dir).write_machine_settings(
        {"camera.crop.width": 400, "camera.crop.height": 400}
    )
    return attrs.evolve(settings, fake_camera_image=CHECKERBOARD_CAMERA_IMAGE)


@pytest.fixture
def delayed_result_ui(
    live_server: LiveServer, tmp_path: Path
) -> Iterator[tuple[LiveUi, DelayedHttp]]:
    with delayed_ui(
        live_server,
        tmp_path,
        method="POST",
        path_suffixes=("/api/jobs/last/apply", "/api/jobs/last/discard"),
    ) as pair:
        yield pair


def _calibrate(page, square_size: str) -> None:
    page.locator("#param-square_size").fill(square_size)
    page.locator("#job-run").click()
    page.locator("#jc-prompt-ok").click()
    expect(page.locator("#jc-status")).to_have_attribute("data-status", "succeeded")
    expect(page.locator("#jc-apply")).to_be_visible()


class TestJobResultActions:
    def test_another_job_hides_the_previous_calibration_actions(
        self, live_ui: LiveUi, browser_page
    ):
        page = browser_page
        page.goto(f"{live_ui.base_url}/posctrl/camera_calibration")
        acquire_control(page)
        _calibrate(page, "10")

        response = page.request.post(
            f"{live_ui.base_url}/api/jobs/job_demo",
            data={"params": {"steps": 1, "interval": 0}},
        )
        assert response.status == 201
        expect(page.locator("#jc-status")).to_have_attribute("data-status", "idle")
        expect(page.locator("#jc-result")).to_be_hidden()
        expect(page.locator("#jc-apply-btn")).to_be_hidden()
        expect(page.locator("#jc-prompt")).to_be_hidden()
        assert (
            page.request.post(f"{live_ui.base_url}/api/jobs/current/abort").status
            == 200
        )

    @pytest.mark.parametrize("action", ["apply", "discard"])
    @pytest.mark.parametrize("delay_response", [False, True])
    def test_delayed_action_cannot_consume_or_hide_a_newer_calibration(
        self,
        live_server: LiveServer,
        delayed_result_ui,
        browser_page,
        action: str,
        delay_response: bool,
    ):
        ui, transport = delayed_result_ui
        page = browser_page
        page.goto(f"{ui.base_url}/posctrl/camera_calibration")
        acquire_control(page)
        _calibrate(page, "10")
        if delay_response:
            transport.delay_response()
        page.locator(f"#jc-{action}-btn").click()
        assert transport.wait_received()
        config_dir = live_server.settings.config_dir
        saved_files = {
            p.name: p.read_bytes() for p in config_dir.iterdir() if p.is_file()
        }

        _calibrate(page, "20")
        with page.expect_response(
            lambda response: response.url.endswith(f"/api/jobs/last/{action}")
        ) as pending:
            transport.resume()
        assert pending.value.status == (200 if delay_response else 409)
        if delay_response:
            expect(page.locator("#toasts")).to_contain_text(
                "設定に反映しました" if action == "apply" else "計測結果を破棄しました"
            )
        else:
            expect(page.locator("#toasts")).to_contain_text("ジョブが切り替わりました")
        expect(page.locator("#jc-apply")).to_be_visible()
        assert {
            p.name: p.read_bytes() for p in config_dir.iterdir() if p.is_file()
        } == saved_files

        # 新しい結果は操作できる。サーバーが消費した事実は WS でも反映される。
        with page.expect_response(
            lambda response: response.url.endswith(f"/api/jobs/last/{action}")
        ) as retried:
            page.locator(f"#jc-{action}-btn").click()
        assert retried.value.status == 200
        expect(page.locator("#jc-apply")).to_be_hidden()
