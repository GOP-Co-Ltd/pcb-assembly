"""マシン操作パネルの「サーボ脱力」（部品実装機）: frontend の中継を通って全サーボのトルクが切れる."""

from collections.abc import Iterator

import pytest
import tomlkit
from playwright.sync_api import expect

from pcbasm.hal import ServoMode
from pcbasm.hal.fake import FakeAudioPlayer, FakeServoBus, ServoCommand
from pcbasm.machine.devices import Devices
from tests.support.browser import LiveServer, LiveUi, acquire_control, start_app
from tests.support.machine_toml import pnp_only
from web.api.app import create_app
from web.api.env import Env
from web.api.machine_config import MachineConfigFile


@pytest.fixture
def bus() -> FakeServoBus:
    bus = FakeServoBus({1: ServoMode.STEP, 2: ServoMode.POSITION})
    bus.set_torque(1, True)
    bus.set_torque(2, True)
    return bus


@pytest.fixture
def live_server(e2e_settings: Env, bus: FakeServoBus) -> Iterator[LiveServer]:
    path = MachineConfigFile(e2e_settings.config_dir).path
    document = tomlkit.parse(pnp_only(path.read_text()))
    servo = tomlkit.table()
    servo["port"] = "/dev/ttyUSB0"
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


class TestServoRelaxButton:
    def test_pressing_it_turns_every_servo_torque_off(
        self, live_ui: LiveUi, browser_page, bus: FakeServoBus
    ):
        page = browser_page
        page.goto(f"{live_ui.base_url}/klipper_status")
        acquire_control(page)
        page.locator("#mc-servo-relax").click()

        expect(page.get_by_test_id("toasts")).to_contain_text("サーボを脱力しました")
        assert ServoCommand(254, "torque", 0) in bus.commands
