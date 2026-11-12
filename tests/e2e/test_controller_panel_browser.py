"""マシン操作パネルのコントローラ操作（Klipper の部品実装機、ピンセットだけの gamepad_control）.

- 「コントローラ操作」で始め、実行中はほかのパネル操作を押せないが、ボタンだけは押せてもう一度押すと終わる
- 始めてすぐ失敗したジョブ（[pnp.servo] が無いなど）は、ジョブのコンソールが無くてもトーストで知らせる

仮想スティック（新ファームの機体）は tests/e2e/test_virtual_stick_browser.py が確かめる。
"""

from collections.abc import Iterator

import httpx
import pytest
import tomlkit
from playwright.sync_api import Page, expect

from pcbasm.hal import ServoMode
from pcbasm.hal.fake import FakeAudioPlayer, FakeGamepad, FakeServoBus
from pcbasm.machine.devices import Devices
from tests.support.browser import LiveServer, LiveUi, acquire_control, start_app
from tests.support.machine_toml import pnp_only
from web.api.app import create_app
from web.api.env import Env
from web.api.machine_config import MachineConfigFile

_SERVO = {
    "port": "/dev/ttyUSB0",
    "rotation_id": 1,
    "open_close_id": 2,
    "closed_angle": 180.0,
    "open_angle": 225.0,
}


@pytest.fixture
def with_servo() -> bool:
    return True


@pytest.fixture
def live_server(e2e_settings: Env, with_servo: bool) -> Iterator[LiveServer]:
    path = MachineConfigFile(e2e_settings.config_dir).path
    document = tomlkit.parse(pnp_only(path.read_text()))
    if with_servo:
        table = tomlkit.table()
        table.update(_SERVO)
        document["pnp"]["servo"] = table  # type: ignore[index]
    path.write_text(tomlkit.dumps(document))
    devices = Devices(
        path,
        e2e_settings.journal_dir,
        audio=FakeAudioPlayer(),
        servo_bus=FakeServoBus(
            {1: ServoMode.STEP, 2: ServoMode.POSITION},
            {2: 2048},
            angle_limits={1: (0, 0), 2: (2048, 2560)},
        ),
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


def _open(page: Page, live_ui: LiveUi) -> None:
    page.goto(f"{live_ui.base_url}/placement")
    acquire_control(page)


class TestControllerButton:
    def test_button_stays_usable_and_ends_the_job(
        self, live_ui: LiveUi, live_server: LiveServer, browser_page: Page
    ):
        page = browser_page
        _open(page, live_ui)
        toggle = page.get_by_test_id("mc-controller-toggle")

        toggle.click()

        expect(toggle).to_have_attribute("aria-pressed", "true")
        expect(page.locator("#mc-relax")).to_be_disabled()
        expect(toggle).to_be_enabled()

        toggle.click()

        expect(toggle).to_have_attribute("aria-pressed", "false")
        expect(page.locator("#mc-relax")).to_be_enabled()
        job = httpx.get(f"{live_server.base_url}/api/jobs/current").json()["job"]
        assert (job["name"], job["status"]) == ("gamepad_control", "aborted")


class TestFailedStart:
    @pytest.fixture
    def with_servo(self) -> bool:
        return False

    def test_failure_is_toasted(self, live_ui: LiveUi, browser_page: Page):
        page = browser_page
        _open(page, live_ui)

        page.get_by_test_id("mc-controller-toggle").click()

        expect(page.get_by_test_id("toasts")).to_contain_text("失敗しました")
        expect(page.get_by_test_id("mc-controller-toggle")).to_have_attribute(
            "aria-pressed", "false"
        )
