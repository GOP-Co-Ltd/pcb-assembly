"""`web.api.routers.pnp.servo` の仕様テスト（部品実装だけの機体、サーボは FakeServoBus）.

POST /api/pnp/servo/relax（マシン操作パネルの「サーボ脱力」）:

- バスの全サーボのトルクをブロードキャストで切る（[pnp.servo] の役割の設定前でも届く）
- 409: 装置が使用中（ジョブ実行中など）は送らない
- 503: [pnp.servo] が無い
- 404: 塗布機（pnp の機能が無い機体）には置かない
- 実機のサーボが脱力することは受け入れ試験（docs/acceptance.md の A24）
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
import tomlkit
from fastapi.testclient import TestClient

from pcbasm.hal import ServoMode
from pcbasm.hal.fake import FakeAudioPlayer, FakeServoBus, ServoCommand
from pcbasm.machine.devices import Devices
from tests.support.machine_toml import pnp_only
from web.api.app import create_app
from web.api.backend import Backend
from web.api.env import Env
from web.api.machine_config import MachineConfigFile

RELAX = "/api/pnp/servo/relax"


def _pnp_machine(path: Path, *, servo: bool) -> None:
    document = tomlkit.parse(pnp_only(path.read_text()))
    if servo:
        table = tomlkit.table()
        table["port"] = "/dev/ttyUSB0"
        document["pnp"]["servo"] = table  # type: ignore[index]
    path.write_text(tomlkit.dumps(document))


@pytest.fixture
def bus() -> FakeServoBus:
    bus = FakeServoBus({1: ServoMode.STEP, 2: ServoMode.POSITION})
    bus.set_torque(1, True)
    bus.set_torque(2, True)
    return bus


@pytest.fixture
def servo_section() -> bool:
    return True


@pytest.fixture
def pnp_client(
    webui_settings: Env, bus: FakeServoBus, servo_section: bool
) -> Iterator[TestClient]:
    path = MachineConfigFile(webui_settings.config_dir).path
    _pnp_machine(path, servo=servo_section)
    devices = Devices(
        path, webui_settings.journal_dir, audio=FakeAudioPlayer(), servo_bus=bus
    )
    with TestClient(create_app(webui_settings, devices=devices)) as client:
        yield client


class TestServoRelax:
    def test_turns_every_servo_torque_off(
        self, pnp_client: TestClient, bus: FakeServoBus
    ):
        response = pnp_client.post(RELAX)

        assert response.status_code == 200
        assert bus.commands[-1] == ServoCommand(254, "torque", 0)
        bus.turn_by_hand(1, 100)  # 脱力しているので手で回せる
        bus.turn_by_hand(2, 200)

    def test_busy_machine_is_409_without_sending(
        self, pnp_client: TestClient, bus: FakeServoBus
    ):
        backend: Backend = pnp_client.app.state.backend  # type: ignore[attr-defined]
        sent = len(bus.commands)
        with backend.lock.hold("pytest-job"):
            response = pnp_client.post(RELAX)

        assert response.status_code == 409
        assert len(bus.commands) == sent

    @pytest.mark.parametrize("servo_section", [False])
    def test_without_pnp_servo_is_503(self, pnp_client: TestClient):
        response = pnp_client.post(RELAX)

        assert response.status_code == 503
        assert "pnp" in response.json()["detail"]


class TestSolderMachine:
    def test_has_no_servo_api(self, client: TestClient):
        assert client.post(RELAX).status_code == 404
