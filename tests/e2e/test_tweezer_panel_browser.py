"""マシン操作パネルのピンセット欄（部品実装機）: frontend の中継を通ってサーボへ指令が届く.

- 回転の ± ボタンは、その角度だけ相対に回す
- 開閉のスライダーは動かしている間も指令を送り、離した位置が最後に届く
- 数値欄は確定した開き具合（0〜1）へ動かす
- スライダーと数値欄は目標（status の target_fraction）、「現在」は実測（open_fraction）を表示する
"""

from collections.abc import Iterator

import pytest
import tomlkit
from playwright.sync_api import Page, expect

from pcbasm.hal import ServoMode
from pcbasm.hal.fake import FakeAudioPlayer, FakeServoBus
from pcbasm.machine.devices import Devices
from tests.support.browser import LiveServer, LiveUi, acquire_control, start_app
from tests.support.machine_toml import pnp_only
from tests.support.polling import wait_until
from web.api.app import create_app
from web.api.env import Env
from web.api.machine_config import MachineConfigFile


@pytest.fixture
def bus() -> FakeServoBus:
    # 閉じ切り 180° = raw 2048、開き切り 225° = raw 2560。Open Close は閉じ切りにいる。
    # 角度制限はサーボ設定ジョブを済ませた状態（Rotation は制限なし、Open Close は閉じ切り〜開き切り）
    return FakeServoBus(
        {1: ServoMode.STEP, 2: ServoMode.POSITION},
        {2: 2048},
        angle_limits={1: (0, 0), 2: (2048, 2560)},
    )


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


def _open_close_gotos(bus: FakeServoBus) -> list[int]:
    return [c.value for c in bus.commands if c.kind == "goto" and c.servo_id == 2]


def _open_panel(page: Page, live_ui: LiveUi) -> None:
    page.goto(f"{live_ui.base_url}/klipper_status")
    acquire_control(page)


def _send_from_field(page: Page, fraction: str) -> None:
    field = page.locator("#mc-open")
    field.fill(fraction)
    field.press("Enter")


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

    def test_dragging_the_slider_follows_and_ends_at_the_released_position(
        self, live_ui: LiveUi, browser_page: Page, bus: FakeServoBus
    ):
        page = browser_page
        # 右下の toast がスライダーに重なってドラッグを奪わないよう、画面を縦に広げる
        page.set_viewport_size({"width": 1280, "height": 1100})
        _open_panel(page, live_ui)
        slider = page.locator("#mc-open-slider")
        slider.scroll_into_view_if_needed()
        box = slider.bounding_box()
        assert box is not None
        y = box["y"] + box["height"] / 2

        # 左端（閉じ切り）からつかみ、右端の外まで少しずつ動かして離す
        page.mouse.move(box["x"] + 2, y)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] + 20, y, steps=20)
        page.mouse.up()

        wait_until(lambda: _open_close_gotos(bus)[-1:] == [2560])
        assert len(_open_close_gotos(bus)) >= 2

    def test_sending_zero_shows_zero_from_the_target(
        self, live_ui: LiveUi, browser_page: Page, bus: FakeServoBus
    ):
        page = browser_page
        _open_panel(page, live_ui)
        _send_from_field(page, "0.5")
        # 脱力していたので、今の位置（閉じ切り 2048）で保持してから送る
        wait_until(lambda: _open_close_gotos(bus) == [2048, 2304])

        _send_from_field(page, "0")

        wait_until(lambda: _open_close_gotos(bus) == [2048, 2304, 2048])
        expect(page.locator("#mc-open")).to_have_value("0.00")
        expect(page.locator("#mc-open-slider")).to_have_value("0")

    def test_current_shows_the_measured_fraction(
        self, live_ui: LiveUi, browser_page: Page, bus: FakeServoBus
    ):
        page = browser_page
        _open_panel(page, live_ui)

        _send_from_field(page, "0.5")

        expect(page.get_by_test_id("mc-open-current")).to_have_text("現在 0.50")
