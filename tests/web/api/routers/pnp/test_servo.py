"""`web.api.routers.pnp.servo` の仕様テスト（部品実装だけの機体、サーボは FakeServoBus）.

POST /api/pnp/servo/relax（マシン操作パネルの「サーボ脱力」）:

- バスの全サーボのトルクをブロードキャストで切る（[pnp.servo] の役割の設定前でも届く）
- 409: 装置が使用中（ジョブ実行中など）は送らない
- 503: [pnp.servo] が無い
- 404: 塗布機（pnp の機能が無い機体）には置かない
- 実機のサーボが脱力することは受け入れ試験（docs/acceptance.md の A24）

POST /api/pnp/servo/rotate・open、GET /api/pnp/servo/status（マシン操作パネルのピンセット欄）:

- rotate は Rotation を degrees だけ相対に、[pnp.servo] の rotation_speed で回す
- open は Open Close を開き具合 fraction（0=閉じ切り、1=開き切り）へ動かす
- status は今の開き具合を返し、トルクには触れない（脱力したサーボを保持しない）
- 409: 使用中は送らない / 422: 範囲外 / 503: [pnp.servo] か役割・角度が未設定
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
ROTATE = "/api/pnp/servo/rotate"
OPEN = "/api/pnp/servo/open"
STATUS = "/api/pnp/servo/status"

# 閉じ切り 180° = raw 2048、開き切り 225° = raw 2560
_ROLES = {
    "rotation_id": 1,
    "open_close_id": 2,
    "closed_angle": 180.0,
    "open_angle": 225.0,
    "rotation_speed": 90.0,
}


def _pnp_machine(path: Path, *, servo: bool, roles: bool = False) -> None:
    document = tomlkit.parse(pnp_only(path.read_text()))
    if servo:
        table = tomlkit.table()
        table["port"] = "/dev/ttyUSB0"
        if roles:
            table.update(_ROLES)
        document["pnp"]["servo"] = table  # type: ignore[index]
    path.write_text(tomlkit.dumps(document))


@pytest.fixture
def bus() -> FakeServoBus:
    # サーボ設定ジョブを済ませた状態（Rotation は制限なし、Open Close は閉じ切り〜開き切り）
    bus = FakeServoBus(
        {1: ServoMode.STEP, 2: ServoMode.POSITION},
        angle_limits={1: (0, 0), 2: (2048, 2560)},
    )
    bus.set_torque(1, True)
    bus.set_torque(2, True)
    return bus


@pytest.fixture
def servo_section() -> bool:
    return True


@pytest.fixture
def roles() -> bool:
    return False


@pytest.fixture
def pnp_client(
    webui_settings: Env, bus: FakeServoBus, servo_section: bool, roles: bool
) -> Iterator[TestClient]:
    path = MachineConfigFile(webui_settings.config_dir).path
    _pnp_machine(path, servo=servo_section, roles=roles)
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


@pytest.mark.parametrize("roles", [True])
class TestTweezerPanel:
    def test_rotate_turns_by_the_degrees_at_the_rotation_speed(
        self, pnp_client: TestClient, bus: FakeServoBus
    ):
        response = pnp_client.post(ROTATE, json={"degrees": -90})

        assert response.status_code == 200
        assert bus.commands[-1] == ServoCommand(1, "step", -1024)
        assert bus.speed(1) == 1024

    @pytest.mark.parametrize(
        ("fraction", "target"), [(0, 2048), (0.5, 2304), (1, 2560)]
    )
    def test_open_goes_to_the_fraction_between_closed_and_open(
        self, pnp_client: TestClient, bus: FakeServoBus, fraction: float, target: int
    ):
        response = pnp_client.post(OPEN, json={"fraction": fraction})

        assert response.status_code == 200
        assert bus.commands[-2:] == [
            ServoCommand(2, "goto", target),
            ServoCommand(2, "torque", 1),
        ]

    def test_status_reports_the_open_fraction_after_open(self, pnp_client: TestClient):
        pnp_client.post(OPEN, json={"fraction": 0.25})

        response = pnp_client.get(STATUS)

        assert response.status_code == 200
        assert response.json() == {
            "open_fraction": pytest.approx(0.25),
            "target_fraction": pytest.approx(0.25),
        }

    def test_status_reports_the_target_apart_from_the_position(
        self, pnp_client: TestClient, bus: FakeServoBus
    ):
        """つまんで閉じ切りの手前で止まっていても、目標は送った開き具合（0）."""
        pnp_client.post(OPEN, json={"fraction": 0})
        bus.set_torque(2, False)
        bus.turn_by_hand(2, 2176)

        response = pnp_client.get(STATUS)

        assert response.json() == {
            "open_fraction": pytest.approx(0.25),
            "target_fraction": 0.0,
        }

    def test_angle_limits_left_by_an_old_setup_are_502_asking_for_the_setup(
        self, pnp_client: TestClient, bus: FakeServoBus
    ):
        bus.set_angle_limits(1, 0, 4095)

        response = pnp_client.post(ROTATE, json={"degrees": 90})

        assert response.status_code == 502
        assert "サーボ設定" in response.json()["detail"]

    def test_status_leaves_relaxed_servos_relaxed(
        self, pnp_client: TestClient, bus: FakeServoBus
    ):
        pnp_client.post(RELAX)
        sent = len(bus.commands)

        assert pnp_client.get(STATUS).status_code == 200

        assert len(bus.commands) == sent
        bus.turn_by_hand(2, 2100)  # 脱力したままなので手で回せる

    def test_rotating_while_gripping_leaves_the_open_close_alone(
        self, pnp_client: TestClient, bus: FakeServoBus
    ):
        """つまんで向きを変えても、Open Close の goal を書き換えない（把持力が抜けない）."""
        pnp_client.post(OPEN, json={"fraction": 0})
        sent = len(bus.commands)

        pnp_client.post(ROTATE, json={"degrees": 10})

        assert [c for c in bus.commands[sent:] if c.servo_id == 2] == []

    def test_status_is_readable_while_the_machine_is_busy(self, pnp_client: TestClient):
        backend: Backend = pnp_client.app.state.backend  # type: ignore[attr-defined]
        with backend.lock.hold("pytest-job"):
            response = pnp_client.get(STATUS)

        assert response.status_code == 200

    @pytest.mark.parametrize(
        ("method", "url", "body"),
        [
            ("POST", ROTATE, {"degrees": 10}),
            ("POST", OPEN, {"fraction": 1}),
            ("GET", STATUS, None),
        ],
    )
    def test_servo_in_the_wrong_mode_is_502(
        self,
        pnp_client: TestClient,
        bus: FakeServoBus,
        method: str,
        url: str,
        body: dict | None,
    ):
        bus.set_mode(2, ServoMode.STEP)

        response = pnp_client.request(method, url, json=body)

        assert response.status_code == 502
        assert "open_close" in response.json()["detail"]

    @pytest.mark.parametrize(
        ("url", "body"),
        [
            (OPEN, {"fraction": -0.01}),
            (OPEN, {"fraction": 1.01}),
            (OPEN, {}),
            (ROTATE, {"degrees": 360.5}),
            (ROTATE, {"degrees": -361}),
            (ROTATE, {}),
        ],
    )
    def test_out_of_range_is_422_without_sending(
        self, pnp_client: TestClient, bus: FakeServoBus, url: str, body: dict
    ):
        sent = len(bus.commands)

        assert pnp_client.post(url, json=body).status_code == 422
        assert len(bus.commands) == sent

    @pytest.mark.parametrize(
        ("url", "body"), [(ROTATE, {"degrees": 10}), (OPEN, {"fraction": 1})]
    )
    def test_busy_machine_is_409_without_sending(
        self, pnp_client: TestClient, bus: FakeServoBus, url: str, body: dict
    ):
        backend: Backend = pnp_client.app.state.backend  # type: ignore[attr-defined]
        sent = len(bus.commands)
        with backend.lock.hold("pytest-job"):
            response = pnp_client.post(url, json=body)

        assert response.status_code == 409
        assert len(bus.commands) == sent


class TestTweezerPanelWithoutRoles:
    @pytest.mark.parametrize(
        ("method", "url", "body"),
        [
            ("POST", ROTATE, {"degrees": 10}),
            ("POST", OPEN, {"fraction": 1}),
            ("GET", STATUS, None),
        ],
    )
    def test_unset_roles_are_503_naming_pnp(
        self, pnp_client: TestClient, method: str, url: str, body: dict | None
    ):
        response = pnp_client.request(method, url, json=body)

        assert response.status_code == 503
        assert "pnp" in response.json()["detail"]


class TestSolderMachine:
    @pytest.mark.parametrize(
        ("method", "url"),
        [("POST", RELAX), ("POST", ROTATE), ("POST", OPEN), ("GET", STATUS)],
    )
    def test_has_no_servo_api(self, client: TestClient, method: str, url: str):
        assert client.request(method, url).status_code == 404
