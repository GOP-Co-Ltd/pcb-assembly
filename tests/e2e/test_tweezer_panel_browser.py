"""マシン操作パネルのピンセット欄（部品実装機）: frontend の中継を通ってサーボへ指令が届く.

- Rotation の ± ボタンは、その角度だけ相対に回す
- Open Close のスライダーと数値欄は、確定した開き具合（0〜1）へ動かす。欄は今の開き具合を表示する
"""

from collections.abc import Iterator

import pytest
import tomlkit
from playwright.sync_api import Page, expect

from pcbasm.hal import ServoMode
from pcbasm.hal.fake import FakeAudioPlayer, FakeServoBus, ServoCommand
from pcbasm.machine.devices import Devices
from tests.support.browser import LiveServer, LiveUi, acquire_control, start_app
from tests.support.machine_toml import pnp_only
from tests.support.polling import wait_until
from web.api.app import create_app
from web.api.env import Env
from web.api.machine_config import MachineConfigFile


@pytest.fixture
def bus() -> FakeServoBus:
    # 閉じ切り 180° = raw 2048、開き切り 225° = raw 2560。Open Close は閉じ切りにいる
    return FakeServoBus({1: ServoMode.STEP, 2: ServoMode.POSITION}, {2: 2048})


@pytest.fixture
def live_server(e2e_settings: Env, bus: FakeServoBus) -> Iterator[LiveServer]:
    path = MachineConfigFile(e2e_settings.config_dir).path
    document = tomlkit.parse(pnp_only(path.read_text()))
    servo = tomlkit.table()
    servo.update(
        {
            "port": "/dev/ttyUSB0",
            "rotation_id": 1,
            "open_close_id": 2,
            "closed_angle": 180.0,
            "open_angle": 225.0,
        }
    )
    document["pnp"]["servo"] = servo  # type: ignore[index]
    path.write_text(tomlkit.dumps(document))
    devices = Devices(
        path, e2e_settings.journal_dir, audio=FakeAudioPlayer(), servo_bus=bus
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


def _steps(bus: FakeServoBus) -> list[int]:
    return [c.value for c in bus.commands if c.kind == "step"]


def _open_panel(page: Page, live_ui: LiveUi) -> None:
    page.goto(f"{live_ui.base_url}/klipper_status")
    acquire_control(page)


class TestTweezerPanel:
    def test_rotate_buttons_turn_by_their_degrees(
        self, live_ui: LiveUi, browser_page: Page, bus: FakeServoBus
    ):
        page = browser_page
        _open_panel(page, live_ui)

        page.locator('[data-mc-rotate="90"]').click()
        wait_until(lambda: _steps(bus) == [1024])
        page.locator('[data-mc-rotate="-10"]').click()

        wait_until(lambda: _steps(bus) == [1024, -114])

    def test_number_field_opens_to_the_fraction_and_shows_it(
        self, live_ui: LiveUi, browser_page: Page, bus: FakeServoBus
    ):
        page = browser_page
        _open_panel(page, live_ui)

        field = page.locator("#mc-open")
        field.fill("0.5")
        field.press("Enter")

        wait_until(lambda: ServoCommand(2, "goto", 2304) in bus.commands)
        # ステータスのポーリングで、欄が今の開き具合を表示する
        expect(page.locator("#mc-open-slider")).to_have_value("0.5")
        field.blur()
        expect(field).to_have_value("0.50")

    def test_slider_opens_to_the_fraction(
        self, live_ui: LiveUi, browser_page: Page, bus: FakeServoBus
    ):
        page = browser_page
        _open_panel(page, live_ui)

        page.locator("#mc-open-slider").fill("1")

        wait_until(lambda: ServoCommand(2, "goto", 2560) in bus.commands)
