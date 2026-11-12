"""`web.api.routers.machine` の、新ファームの機体（``[pnp.motion]``）での仕様テスト.

ステージは FakeMotionStage を注入する。塗布機（Klipper）の経路は test_machine.py が持つ。

- POST /api/machine/command は命令を MotionStage で実行し、終わった後の位置と原点出しの済んだ軸を返す
  - 400: 可動域の外・原点出しをしていない軸・この機体で使えない命令（焦点 Z・キャップ位置・任意 G-code）
  - 409: 装置が使用中 / 502: ステージが shutdown している
- GET /api/machine/status は常に 200。開いていないステージには接続せず ``connected=false`` を返す。
  shutdown・切断・壊れた ``[pnp.motion]`` も ``connected=false`` と理由
- GET /api/machine/limits はステージの可動域（``[pnp.motion]`` から読み、開いていないステージに接続しない）
- POST /api/machine/firmware-restart はステージを restart するだけで、WebUI のサービスは再起動しない
- POST /api/emergency-stop は開いているステージを止める
- 実機の MCU での確かめは受け入れ試験（docs/acceptance.md）
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pcbasm.hal import ContactSettings, MotionState, Velocity
from pcbasm.hal.fake import FakeAudioPlayer, FakeMotionStage
from tests.support.polling import wait_until
from tests.support.selfupdate import UpdateSandbox, make_update_sandbox
from tests.support.web import fake_motion_stage, motion_devices, register_gated
from web.api.app import create_app
from web.api.backend import Backend
from web.api.env import Env
from web.selfupdate.runner import UpdateRunner

COMMAND = "/api/machine/command"
STATUS = "/api/machine/status"


@pytest.fixture
def stage() -> FakeMotionStage:
    return fake_motion_stage()


@pytest.fixture
def sandbox(tmp_path: Path) -> UpdateSandbox:
    return make_update_sandbox(tmp_path / "selfupdate-sandbox")


@pytest.fixture
def motion_client(
    webui_settings: Env, stage: FakeMotionStage, sandbox: UpdateSandbox
) -> Iterator[TestClient]:
    devices = motion_devices(webui_settings, FakeAudioPlayer(), stage)
    app = create_app(
        webui_settings, devices=devices, update_runner=UpdateRunner(sandbox.settings)
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def unopened_client(webui_settings: Env) -> Iterator[TestClient]:
    """ステージを注入しない新ファームの機体（MCU を開く口は root conftest が塞いでいる）."""
    devices = motion_devices(webui_settings, FakeAudioPlayer(), None)
    with TestClient(create_app(webui_settings, devices=devices)) as client:
        yield client


def _home(client: TestClient) -> None:
    assert client.post(COMMAND, json={"type": "home"}).status_code == 200


class TestMotionCommand:
    def test_home_homes_every_axis_and_returns_the_position(
        self, motion_client: TestClient, stage: FakeMotionStage
    ):
        response = motion_client.post(COMMAND, json={"type": "home", "axes": []})

        assert response.status_code == 200, response.text
        assert response.json() == {
            "connected": True,
            "position": {"x": 0.0, "y": 0.0, "z": 0.0},
            "position_label": "X0.000 Y0.000 Z0.000",
            "homed_axes": "xyz",
            "error": None,
            "load_label": None,
        }
        assert stage.commands == ["home z", "home x", "home y"]

    def test_move_goes_to_the_coordinates(
        self, motion_client: TestClient, stage: FakeMotionStage
    ):
        _home(motion_client)

        response = motion_client.post(COMMAND, json={"type": "move", "x": 12.5})

        assert response.status_code == 200, response.text
        assert response.json()["position_label"] == "X12.500 Y0.000 Z0.000"
        assert stage.status().position.x == 12.5

    def test_jog_moves_by_the_distance(
        self, motion_client: TestClient, stage: FakeMotionStage
    ):
        _home(motion_client)

        response = motion_client.post(
            COMMAND, json={"type": "jog", "axis": "z", "distance": -10.0}
        )

        assert response.status_code == 200, response.text
        assert stage.status().position.z == -10.0

    def test_relax_clears_the_homed_axes(
        self, motion_client: TestClient, stage: FakeMotionStage
    ):
        _home(motion_client)

        response = motion_client.post(COMMAND, json={"type": "relax"})

        assert response.status_code == 200, response.text
        assert response.json()["homed_axes"] == ""
        assert response.json()["position_label"] == "X? Y? Z?"

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"type": "jog", "axis": "x", "distance": 1.0}, id="unhomed"),
            pytest.param({"type": "move", "x": 100.0}, id="outside"),
        ],
    )
    def test_impossible_move_is_400(
        self, motion_client: TestClient, stage: FakeMotionStage, body: dict[str, object]
    ):
        if body["type"] == "move":
            _home(motion_client)
        sent = stage.commands

        response = motion_client.post(COMMAND, json=body)

        assert response.status_code == 400
        assert response.json()["detail"]
        assert stage.commands == sent

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"type": "focus_z"}, id="focus_z"),
            pytest.param({"type": "move_to_cap"}, id="move_to_cap"),
            pytest.param({"type": "gcode", "gcode": "G28"}, id="gcode"),
        ],
    )
    def test_klipper_only_command_is_400(
        self, motion_client: TestClient, stage: FakeMotionStage, body: dict[str, object]
    ):
        response = motion_client.post(COMMAND, json=body)

        assert response.status_code == 400
        assert "この機体では使えません" in response.json()["detail"]
        assert stage.commands == []

    def test_busy_machine_is_409(
        self, motion_client: TestClient, stage: FakeMotionStage
    ):
        backend: Backend = motion_client.app.state.backend  # type: ignore[attr-defined]
        with backend.lock.hold("pytest-job"):
            response = motion_client.post(COMMAND, json={"type": "home"})

        assert response.status_code == 409
        assert stage.commands == []

    def test_shutdown_stage_is_502_with_the_reason(
        self, motion_client: TestClient, stage: FakeMotionStage
    ):
        stage.shutdown("Timer too close")

        response = motion_client.post(COMMAND, json={"type": "home"})

        assert response.status_code == 502
        assert "Timer too close" in response.json()["detail"]


class TestMotionTare:
    """タール（ロードセルのある新ファームの機体だけ）."""

    @pytest.fixture
    def stage(self) -> FakeMotionStage:
        return fake_motion_stage(load_cell=True)

    def test_zeroes_the_load_and_returns_it(
        self, motion_client: TestClient, stage: FakeMotionStage
    ):
        stage.set_load(7.25)
        assert motion_client.get(STATUS).json()["load_label"] == "荷重 7.2 g"

        response = motion_client.post(COMMAND, json={"type": "tare"})

        assert response.status_code == 200, response.text
        assert response.json()["load_label"] == "荷重 0.0 g"
        assert stage.status().load == 0.0


class TestMotionContact:
    """接触保持の間（ロードセルの接触で止まり、まだ離していない）."""

    @pytest.fixture
    def stage(self) -> FakeMotionStage:
        return fake_motion_stage(homed=True, load_cell=True, contact=ContactSettings())

    def _press(self, stage: FakeMotionStage) -> None:
        stage.set_velocity(Velocity(z=-1.0))
        stage.set_load(60.0)

    def test_status_shows_the_contact(
        self, motion_client: TestClient, stage: FakeMotionStage
    ):
        self._press(stage)

        assert motion_client.get(STATUS).json()["load_label"] == "荷重 60.0 g（接触中）"

    def test_xy_jog_is_refused_with_the_reason(
        self, motion_client: TestClient, stage: FakeMotionStage
    ):
        self._press(stage)

        response = motion_client.post(
            COMMAND, json={"type": "jog", "axis": "x", "distance": 1.0}
        )

        assert response.status_code == 400
        assert "接触中" in response.json()["detail"]


class TestMotionStatus:
    def test_unhomed_axes_are_shown_as_unknown(self, motion_client: TestClient):
        response = motion_client.get(STATUS)

        assert response.status_code == 200
        data = response.json()
        assert data["connected"] is True
        assert data["position_label"] == "X? Y? Z?"
        assert data["homed_axes"] == ""

    def test_follows_the_stage_position(
        self, motion_client: TestClient, stage: FakeMotionStage
    ):
        stage.home()
        stage.move_to(x=3.0, y=4.0, z=-5.0)

        data = motion_client.get(STATUS).json()

        assert data["position"] == {"x": 3.0, "y": 4.0, "z": -5.0}
        assert data["position_label"] == "X3.000 Y4.000 Z-5.000"
        assert data["homed_axes"] == "xyz"

    def test_without_a_load_cell_has_no_load_label(self, motion_client: TestClient):
        assert motion_client.get(STATUS).json()["load_label"] is None

    def test_tare_without_a_load_cell_is_400(
        self, motion_client: TestClient, stage: FakeMotionStage
    ):
        response = motion_client.post(COMMAND, json={"type": "tare"})

        assert response.status_code == 400
        assert "ロードセル" in response.json()["detail"]
        assert stage.commands == []

    def test_shutdown_is_not_connected_with_the_reason(
        self, motion_client: TestClient, stage: FakeMotionStage
    ):
        stage.shutdown("Timer too close")

        response = motion_client.get(STATUS)

        assert response.status_code == 200
        assert response.json()["connected"] is False
        assert "Timer too close" in response.json()["error"]

    def test_disconnected_stage_is_not_connected(
        self, motion_client: TestClient, stage: FakeMotionStage
    ):
        stage.disconnect()

        response = motion_client.get(STATUS)

        assert response.status_code == 200
        assert response.json()["connected"] is False
        assert response.json()["error"]

    def test_unopened_stage_is_not_connected_without_opening_it(
        self, unopened_client: TestClient
    ):
        """開いていないステージには接続しない（開けば root conftest がテストを落とす）."""
        response = unopened_client.get(STATUS)

        assert response.status_code == 200
        assert response.json()["connected"] is False
        assert "接続していません" in response.json()["error"]

    def test_broken_motion_section_is_a_config_error_with_the_reason(
        self, unopened_client: TestClient, webui_settings: Env
    ):
        """Klipper の機体と同じく、machine.toml の誤りは接続の不通ではなく設定エラー（503）にする."""
        path = webui_settings.config_dir / "machine.toml"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                'kinematics = "inversed_corexy"', 'kinematics = "delta"'
            ),
            encoding="utf-8",
        )

        response = unopened_client.get(STATUS)

        assert response.status_code == 503
        assert "pnp.motion" in response.json()["detail"]


class TestMotionLimits:
    def test_returns_the_stage_limits(self, motion_client: TestClient):
        response = motion_client.get("/api/machine/limits")

        assert response.status_code == 200
        assert response.json() == {
            "x": {"min": 0.0, "max": 68.0},
            "y": {"min": 0.0, "max": 50.0},
            "z": {"min": -40.0, "max": 0.0},
        }

    def test_does_not_open_the_stage(self, unopened_client: TestClient):
        """ページを開くたびに呼ばれるので、MCU に接続しない（開けば root conftest がテストを落とす）."""
        response = unopened_client.get("/api/machine/limits")

        assert response.status_code == 200
        assert response.json()["x"] == {"min": 0.0, "max": 68.0}
        assert "接続していません" in unopened_client.get(STATUS).json()["error"]


class TestMotionEmergencyStop:
    def test_stops_the_stage(self, motion_client: TestClient, stage: FakeMotionStage):
        response = motion_client.post("/api/emergency-stop")

        assert response.status_code == 200
        assert stage.status().state is MotionState.SHUTDOWN


class TestMotionFirmwareRestart:
    def test_restarts_only_the_stage(
        self,
        motion_client: TestClient,
        stage: FakeMotionStage,
        sandbox: UpdateSandbox,
    ):
        stage.home()
        stage.emergency_stop()

        response = motion_client.post("/api/machine/firmware-restart")

        assert response.status_code == 200, response.text
        assert response.json()["warning"] == ""
        assert "原点出し" in response.json()["message"]
        status = stage.status()
        assert status.state is MotionState.IDLE
        assert status.homed == frozenset()
        assert sandbox.restarts() == []

    def test_aborts_the_running_job(self, motion_client: TestClient):
        backend: Backend = motion_client.app.state.backend  # type: ignore[attr-defined]
        register_gated(backend.catalog, name="restart_target")
        assert (
            motion_client.post("/api/jobs/restart_target", json={}).status_code == 201
        )

        assert motion_client.post("/api/machine/firmware-restart").status_code == 200

        wait_until(
            lambda: motion_client.get("/api/jobs/current").json()["job"]["status"]
            == "aborted",
            timeout=10.0,
        )
