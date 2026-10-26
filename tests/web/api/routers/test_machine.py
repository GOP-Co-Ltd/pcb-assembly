"""`web.api.routers.machine` の仕様テスト.

マシン操作パネル（POST /api/machine/command）:

- 命令は `web.contract.models.MachineCommandRequest`（type で判別）。形の誤り（jog の axis/distance 欠落、未知の type）は 422
- 400: 実行できない命令（軸の無い move）/ focus_z 不可（z_position なし）/ limits 超過
- 409: machine_lock 取得失敗（detail に owner）
- 502: Moonraker 接続不能（テスト用 config の port 7126 への実接続で検証、モック不使用）
- 200: 実機での操作完了は受け入れ試験（docs/acceptance.md の A1）で確かめる

- type="gcode": gcode 欠落・空文字・空白だけは 422、送信成功系は受け入れ試験（A1）

- type="move_to_cap" はキャップ位置が未記録なら 400 で断る

設定エラー（degraded）の契約: x/y/z が揃っていない `[solder.nozzle.cap]` は
未記録ではなく設定エラーとして 503 で断り、理由を返す（原点や PRESENT へ動かさない）。
設定を読まない命令（ホーミング・ジョグ・脱力・任意 G-code）は設定エラーでも送れる

Klipper ステータス・可動域・緊急停止・ファームウェア再起動:

- GET /api/machine/status は常に 200。接続失敗は connected=false + error。位置の表示文字列 position_label も返す
- POST /api/emergency-stop は接続不能で 502（ロック・操作権に妨げられないことは tests/repo/test_update_contract.py）
- POST /api/machine/firmware-restart は接続不能で 502、ロック非経由、
  先頭で jobs.request_abort()
- 実 Moonraker での status と、emergency_stop / firmware_restart の成功経路は
  受け入れ試験（docs/acceptance.md の A1、A14）で確かめる
- E-STOP は先頭で jobs.request_abort() を呼ぶ（Moonraker 不達で 502 でも
  実行中ジョブは ABORTED になる）
- GET /api/machine/limits は XYZStage.limits を返す。Moonraker 不通と printer.cfg の不備は 502
"""

import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcbasm.hal.fake import FAKE_PRINTER_CONFIG, FakeAudioPlayer, FakeKlipper
from tests.support.selfupdate import UpdateSandbox, make_update_sandbox
from tests.support.web import devices_with_audio, register_gated
from web.api.app import create_app
from web.api.backend import Backend
from web.api.env import Env
from web.selfupdate.runner import UpdateRunner


@pytest.mark.usefixtures("unreachable_klipper")
class TestMachineControlValidation:
    """命令の検証（Moonraker 接続前に 422 / 400）."""

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"type": "jog", "axis": "x"}, id="jog-without-distance"),
            pytest.param({"type": "jog", "distance": 1.0}, id="jog-without-axis"),
            pytest.param({"type": "record"}, id="unknown-type"),
            pytest.param({"action": "relax"}, id="without-type"),
        ],
    )
    def test_malformed_command_returns_422(
        self, client: TestClient, body: dict[str, object]
    ):
        response = client.post("/api/machine/command", json=body)

        assert response.status_code == 422

    def test_move_without_axes_returns_400(self, client: TestClient):
        response = client.post("/api/machine/command", json={"type": "move"})

        assert response.status_code == 400

    def test_focus_z_without_z_position_returns_400(
        self, client: TestClient, config_dir: Path
    ):
        calibration = config_dir / "ov9281_test_fixture.json"
        data = json.loads(calibration.read_text(encoding="utf-8"))
        data["z_position"] = None
        calibration.write_text(json.dumps(data), encoding="utf-8")

        response = client.post("/api/machine/command", json={"type": "focus_z"})

        assert response.status_code == 400


class TestKlipperErrors:
    """Klipper へ届かない・断られた操作は 502 で理由を返す."""

    def test_refused_send_returns_502_with_moonraker_reason(
        self, client: TestClient, mount_klipper: Callable[[FakeKlipper], None]
    ):
        klipper = FakeKlipper()
        klipper.fail_on(lambda gcode: True)
        mount_klipper(klipper)

        response = client.post("/api/machine/command", json={"type": "relax"})

        assert response.status_code == 502
        assert response.json() == {"detail": "Bad Request"}


@pytest.mark.usefixtures("unreachable_klipper")
class TestJournal:
    """REST の操作は当日の G-code ジャーナルに残る."""

    def test_send_is_journaled_even_when_moonraker_is_down(
        self, client: TestClient, webui_settings: Env
    ):
        response = client.post(
            "/api/machine/command", json={"type": "home", "axes": ["x"]}
        )

        assert response.status_code == 502
        [journal] = webui_settings.journal_dir.glob("*-rest.gcode")
        assert (
            journal.read_text(encoding="utf-8") == "# send timeout=60.0\nG28 X\nM400\n"
        )


class TestGcodeCommand:
    """`type="gcode"`（dev タブの任意 G-code 送信）."""

    @pytest.mark.usefixtures("unreachable_klipper")
    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"type": "gcode"}, id="missing-gcode"),
            pytest.param({"type": "gcode", "gcode": ""}, id="empty-gcode"),
            pytest.param({"type": "gcode", "gcode": " \n\t"}, id="whitespace-gcode"),
        ],
    )
    def test_blank_gcode_returns_422(self, client: TestClient, body: dict[str, object]):
        response = client.post("/api/machine/command", json=body)

        assert response.status_code == 422


@pytest.mark.usefixtures("unreachable_klipper")
class TestMoveToCap:
    """`type="move_to_cap"`.

    キャップ未記録は 400。記録済みなら G-code 送信まで到達し、テスト用 config の Klipper（port 7126
    非リッスン）で 502 になる = バリデーション通過の証明。
    """

    def test_unrecorded_cap_returns_400(self, client: TestClient):
        response = client.post("/api/machine/command", json={"type": "move_to_cap"})

        assert response.status_code == 400
        assert "ノズルキャップ" in response.text

    def test_partially_recorded_cap_is_refused_as_a_config_error(
        self, partial_nozzle_cap: Path, client: TestClient
    ):
        """X だけ書かれた `[solder.nozzle.cap]` は設定エラーとして 503 で断る.

        未記録として扱って原点や PRESENT へ動かさない。理由は応答に載せる。
        """
        response = client.post("/api/machine/command", json={"type": "move_to_cap"})

        assert response.status_code == 503
        assert "x / y / z の 3 つ" in response.json()["detail"]

    def test_commands_without_config_are_sent_despite_a_config_error(
        self, partial_nozzle_cap: Path, client: TestClient
    ):
        """設定を読まない脱力は、設定エラーでも Klipper まで届く（不達の 502 = 送信を試みた証明）."""
        response = client.post("/api/machine/command", json={"type": "relax"})

        assert response.status_code == 502

    def test_recorded_cap_passes_validation_and_returns_502(
        self, client: TestClient, config_dir: Path
    ):
        path = config_dir / "machine.toml"
        path.write_text(
            path.read_text(encoding="utf-8")
            + "\n[solder.nozzle.cap]\nx = 10.0\ny = 20.0\nz = 3.5\n",
            encoding="utf-8",
        )

        response = client.post("/api/machine/command", json={"type": "move_to_cap"})

        assert response.status_code == 502


class TestMachineControlExclusion:
    """ジョブ共有ロックとの排他."""

    def test_busy_returns_409_with_owner_in_detail(
        self, client: TestClient, backend: Backend
    ):
        with backend.lock.hold("pytest-job"):
            response = client.post("/api/machine/command", json={"type": "relax"})

        assert response.status_code == 409
        assert "pytest-job" in response.text


@pytest.mark.usefixtures("unreachable_klipper")
class TestMachineControlMoonrakerDown:
    """Moonraker 不達（port 7126 = 非リッスン、実接続で検証）."""

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"type": "home"}, id="home"),
            pytest.param({"type": "relax"}, id="relax"),
            pytest.param({"type": "gcode", "gcode": "M400"}, id="gcode"),
        ],
    )
    def test_unreachable_moonraker_returns_502(
        self, client: TestClient, body: dict[str, object]
    ):
        response = client.post("/api/machine/command", json=body)

        assert response.status_code == 502


class TestKlipperStatus:
    """GET /api/machine/status."""

    @pytest.mark.usefixtures("unreachable_klipper")
    def test_unreachable_moonraker_returns_200_with_connected_false(
        self, client: TestClient
    ):
        response = client.get("/api/machine/status")

        assert response.status_code == 200
        data = response.json()
        assert data["connected"] is False
        assert data["error"]
        assert data["position"] is None

    def test_position_label_is_built_by_the_server(
        self, client: TestClient, mount_klipper: Callable[[FakeKlipper], None]
    ):
        """表示文字列はサーバーが組む（JS に座標の書式を持たせない）."""
        klipper = FakeKlipper()
        klipper.set_status("gcode_move", "gcode_position", [10.0, 20.5, -3.25, 0.0])
        mount_klipper(klipper)

        data = client.get("/api/machine/status").json()

        assert data["position"] == {"x": 10.0, "y": 20.5, "z": -3.25}
        assert data["position_label"] == "X10.000 Y20.500 Z-3.250"


@pytest.mark.usefixtures("unreachable_klipper")
class TestEmergencyStop:
    """POST /api/emergency-stop."""

    def test_unreachable_moonraker_returns_502(self, client: TestClient):
        response = client.post("/api/emergency-stop")

        assert response.status_code == 502

    def test_aborts_running_job_even_when_moonraker_unreachable(
        self, client: TestClient, app: FastAPI
    ):
        # E-STOP の先頭で jobs.request_abort()（Klipper 送信失敗でも
        # abort フラグは立つ）
        register_gated(app.state.backend.catalog, name="estop_target")
        assert client.post("/api/jobs/estop_target", json={}).status_code == 201

        assert client.post("/api/emergency-stop").status_code == 502

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            job = client.get("/api/jobs/current").json()["job"]
            if job is not None and job["status"] == "aborted":
                return
            time.sleep(0.02)
        pytest.fail("E-STOP 後にジョブが aborted になりませんでした")


@pytest.mark.usefixtures("unreachable_klipper")
class TestFirmwareRestart:
    """POST /api/machine/firmware-restart."""

    def test_unreachable_moonraker_returns_502(self, client: TestClient):
        response = client.post("/api/machine/firmware-restart")

        assert response.status_code == 502

    def test_bypasses_machine_lock(self, client: TestClient, backend: Backend):
        # firmware restart はロック非経由: busy 中でも 409 にはならない
        # （Moonraker 不達のため 502 に到達する = ロックで弾かれていない）
        with backend.lock.hold("pytest-job"):
            response = client.post("/api/machine/firmware-restart")

        assert response.status_code == 502

    def test_aborts_running_job_even_when_moonraker_unreachable(
        self, client: TestClient, app: FastAPI
    ):
        # firmware restart の先頭で jobs.request_abort()。
        # Klipper 送信が 502 でも実行中ジョブは abort へ進む。
        register_gated(app.state.backend.catalog, name="firmware_restart_target")
        response = client.post("/api/jobs/firmware_restart_target", json={})
        assert response.status_code == 201

        assert client.post("/api/machine/firmware-restart").status_code == 502

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            job = client.get("/api/jobs/current").json()["job"]
            if job is not None and job["status"] == "aborted":
                return
            time.sleep(0.02)
        pytest.fail("firmware restart 後にジョブが aborted になりませんでした")


class TestStageLimits:
    """GET /api/machine/limits."""

    @pytest.mark.usefixtures("unreachable_klipper")
    def test_unreachable_moonraker_returns_502(self, client: TestClient):
        response = client.get("/api/machine/limits")

        assert response.status_code == 502

    def test_missing_printer_cfg_section_returns_502_with_the_reason(
        self, client: TestClient, mount_klipper: Callable[[FakeKlipper], None]
    ):
        config = {k: v for k, v in FAKE_PRINTER_CONFIG.items() if k != "printer"}
        mount_klipper(FakeKlipper(config))

        response = client.get("/api/machine/limits")

        assert response.status_code == 502
        assert "printer.cfgに[printer]セクション" in response.json()["detail"]


@pytest.mark.usefixtures("unreachable_klipper")
class TestFirmwareRestartAlsoRestartsServices:
    """ファームウェア再起動は WebUI のサービス（api / ui）も再起動する.

    成功経路は実 Moonraker が要るので、受け入れ試験（A14）で確かめる。

    ここで固定するのは、Klipper へ送れなかったときに unit を落とさないこと。

    落とすと、装置が応答しない状態で画面まで一緒に消える。
    """

    @pytest.fixture
    def sandbox(self, tmp_path: Path) -> UpdateSandbox:
        return make_update_sandbox(tmp_path / "selfupdate")

    @pytest.fixture
    def restart_client(
        self,
        webui_settings: Env,
        paste_test_board_footprint_root: Path,
        sandbox: UpdateSandbox,
    ) -> Iterator[TestClient]:
        app = create_app(
            webui_settings,
            devices=devices_with_audio(webui_settings, FakeAudioPlayer()),
            paste_test_board_footprint_root=paste_test_board_footprint_root,
            update_runner=UpdateRunner(sandbox.settings),
        )
        with TestClient(app) as test_client:
            yield test_client

    def test_unreachable_moonraker_leaves_the_services_running(
        self, restart_client: TestClient, sandbox: UpdateSandbox
    ):
        assert restart_client.post("/api/machine/firmware-restart").status_code == 502

        assert sandbox.restarts() == []
