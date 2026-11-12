"""新ファームの機体（``[pnp.motion]``）のマシン操作パネル: frontend の中継を通ってステージが動き、位置が表示に出る.

- 全軸の原点出しのあと座標欄に値を入れて確定すると、その座標へ動き、パネルの位置表示に出る
- 焦点 Z とキャップ位置のボタンは出さない（押せるものが無いので、JS もそれで止まらない）
"""

from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

from pcbasm.hal.fake import FakeAudioPlayer, FakeMotionStage
from tests.support.browser import LiveServer, LiveUi, acquire_control, start_app
from tests.support.polling import wait_until
from tests.support.web import fake_motion_stage, motion_devices
from web.api.app import create_app
from web.api.env import Env


@pytest.fixture
def stage() -> FakeMotionStage:
    return fake_motion_stage()


@pytest.fixture
def live_server(e2e_settings: Env, stage: FakeMotionStage) -> Iterator[LiveServer]:
    devices = motion_devices(e2e_settings, FakeAudioPlayer(), stage)
    server = start_app(create_app(e2e_settings, devices=devices))
    try:
        yield LiveServer(
            base_url=f"http://127.0.0.1:{server.port}",
            settings=e2e_settings,
            port=server.port,
        )
    finally:
        server.stop()


class TestMotionPanel:
    def test_home_then_move_shows_the_new_position(
        self, live_ui: LiveUi, browser_page: Page, stage: FakeMotionStage
    ):
        page = browser_page
        page.goto(f"{live_ui.base_url}/klipper_status")
        acquire_control(page)
        expect(page.locator("#mc-focus-z")).to_have_count(0)

        page.locator(".mc-home-all").click()
        wait_until(lambda: stage.status().homed == frozenset({"x", "y", "z"}))
        expect(page.locator("#mc-position")).to_have_text("位置: X0.000 Y0.000 Z0.000")

        field = page.locator("#mc-x")
        field.fill("12.5")
        field.press("Enter")

        wait_until(lambda: stage.status().position.x == 12.5)
        expect(page.locator("#mc-position")).to_have_text("位置: X12.500 Y0.000 Z0.000")
        expect(page.locator("#mc-homed")).to_have_text("homed: xyz")
