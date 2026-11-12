"""画面の仮想スティック（コントローラ操作のジョブ、新ファームの機体）: frontend の中継と WS を通ってステージへ速度が届く.

- XY パッドを押して倒している間は速度を送り、離すと 0 になる（右 = X+、上 = Y−）
- 押したまま送信が途切れると、backend は届いてから VIRTUAL_TIMEOUT を過ぎたところで 0 にする。
  ブラウザの時計を止めて送信を途切れさせ、backend の時計は実時間で待つ（page.clock では backend の時計は進まない）
"""

import time
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from pcbasm.hal import Velocity
from pcbasm.hal.fake import FakeAudioPlayer, FakeGamepad, FakeMotionStage
from pcbasm.machine.devices import Devices
from pcbasm.pnp.gamepad_control import VIRTUAL_TIMEOUT
from tests.support.browser import LiveServer, LiveUi, acquire_control, start_app
from tests.support.polling import wait_until
from tests.support.web import fake_motion_stage, use_motion_machine
from web.api.app import create_app
from web.api.env import Env


@pytest.fixture
def stage() -> FakeMotionStage:
    return fake_motion_stage(homed=True)


@pytest.fixture
def live_server(e2e_settings: Env, stage: FakeMotionStage) -> Iterator[LiveServer]:
    path = use_motion_machine(e2e_settings.config_dir)
    devices = Devices(
        path,
        e2e_settings.journal_dir,
        audio=FakeAudioPlayer(),
        motion=stage,
        gamepad=FakeGamepad(),
    )
    server = start_app(create_app(e2e_settings, devices=devices))
    try:
        yield LiveServer(
            base_url=f"http://127.0.0.1:{server.port}",
            settings=e2e_settings,
            port=server.port,
        )
    finally:
        # ジョブは中止されるまで続くので、止めてからサーバーを落とす
        httpx.post(f"http://127.0.0.1:{server.port}/api/jobs/current/abort")
        server.stop()


def _last(stage: FakeMotionStage) -> Velocity:
    velocities = stage.velocities
    return velocities[-1][1] if velocities else Velocity()


def _start(page: Page, live_ui: LiveUi) -> None:
    """ジョブを始め、XY パッドの中心を押して右上へ倒す（押したまま返る）."""
    page.goto(f"{live_ui.base_url}/gamepad_motion")
    acquire_control(page)
    page.get_by_test_id("job-run").click()
    expect(page.get_by_test_id("jc-status")).to_have_attribute("data-status", "running")
    box = page.get_by_test_id("vs-pad").bounding_box()
    assert box is not None
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx + box["width"] / 4, cy - box["height"] / 4, steps=5)


class TestVirtualStick:
    def test_drag_moves_the_stage_and_release_stops_it(
        self, live_ui: LiveUi, browser_page: Page, stage: FakeMotionStage
    ):
        page = browser_page
        _start(page, live_ui)

        wait_until(lambda: _last(stage).x > 0.0 and _last(stage).y < 0.0)

        page.mouse.up()
        wait_until(lambda: _last(stage) == Velocity())

    def test_stops_after_the_timeout_when_sending_stalls(
        self, live_ui: LiveUi, browser_page: Page, stage: FakeMotionStage
    ):
        page = browser_page
        page.clock.install()
        _start(page, live_ui)
        wait_until(lambda: _last(stage).x > 0.0)

        # 押したまま、ブラウザの時計を止めて送信を途切れさせる（止める瞬間に期限の来たタイマーは 1 回だけ走る）
        stalled_at = time.monotonic()
        page.clock.pause_at(page.evaluate("Date.now()") + 1000)

        wait_until(lambda: _last(stage) == Velocity())
        stopped_at = next(
            t for t, v in stage.velocities if t > stalled_at and v == Velocity()
        )
        assert stopped_at - stalled_at >= VIRTUAL_TIMEOUT - 0.1
