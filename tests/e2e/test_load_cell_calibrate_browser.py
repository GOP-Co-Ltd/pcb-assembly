"""ロードセルの校正（load_cell_calibrate、新ファームの機体）: ジョブの手順を進めて Apply すると、設定ページの値が変わる."""

from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

from pcbasm.hal.fake import FakeAudioPlayer, FakeMotionStage
from pcbasm.machine.devices import Devices
from tests.support.browser import LiveServer, LiveUi, acquire_control, start_app
from tests.support.machine_toml import motion_table, pnp_motion
from tests.support.web import fake_motion_stage
from web.api.app import create_app
from web.api.env import Env
from web.api.machine_config import MachineConfigFile


@pytest.fixture
def stage() -> FakeMotionStage:
    return fake_motion_stage(homed=True, load_cell=True, counts_per_gram=2000.0)


@pytest.fixture
def live_server(e2e_settings: Env, stage: FakeMotionStage) -> Iterator[LiveServer]:
    path = MachineConfigFile(e2e_settings.config_dir).path
    motion = motion_table()
    motion["load_cell"] = {
        "sclk_pin": "gpio0",
        "dout_pin": "gpio1",
        "counts_per_gram": 2666.98,
    }
    path.write_text(pnp_motion(path.read_text(), motion))
    devices = Devices(
        path, e2e_settings.journal_dir, audio=FakeAudioPlayer(), motion=stage
    )
    server = start_app(create_app(e2e_settings, devices=devices))
    try:
        yield LiveServer(
            base_url=f"http://127.0.0.1:{server.port}",
            settings=e2e_settings,
            port=server.port,
        )
    finally:
        server.stop()


class TestLoadCellCalibrate:
    def test_apply_changes_the_setting_page(
        self, live_ui: LiveUi, browser_page: Page, stage: FakeMotionStage
    ):
        page = browser_page
        page.goto(f"{live_ui.base_url}/load_cell_calibrate")
        acquire_control(page)
        page.get_by_test_id("job-run").click()

        ok = page.get_by_test_id("jc-prompt-ok")
        expect(ok).to_have_text("タール")
        ok.click()
        expect(ok).to_have_text("測る")
        stage.set_load(100.0)
        ok.click()
        page.get_by_test_id("jc-prompt-input").fill("100")
        ok.click()
        expect(page.get_by_test_id("jc-status")).to_have_attribute(
            "data-status", "succeeded"
        )
        page.get_by_test_id("jc-apply-btn").click()
        expect(page.get_by_test_id("jc-apply")).to_be_hidden()

        page.goto(f"{live_ui.base_url}/machine_config")
        page.get_by_test_id("settings-nav-pnp").click()
        expect(
            page.get_by_test_id("ms-pnp.motion.load_cell.counts_per_gram")
        ).to_have_value("2000.0")
