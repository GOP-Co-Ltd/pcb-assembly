"""`web.api.routers.system` の仕様テスト.

計画書「routers」節:

- GET /api/klipper/status は常に 200。接続失敗は connected=false + error
- POST /api/emergency-stop は接続不能で 502（ロック・操作権に妨げられないことは tests/repo/test_update_contract.py）
- POST /api/firmware-restart は接続不能で 502、ロック非経由、
  先頭で jobs.request_abort()
- 実 Moonraker での status と、emergency_stop / firmware_restart の成功経路は
  受け入れ試験（docs/acceptance.md の A1、A14）で確かめる

Phase 3 追記（計画書 webui-phase3.md「既存ルーター・app への変更」節）:

- E-STOP は先頭で jobs.request_abort() を呼ぶ（Moonraker 不達で 502 でも
  実行中ジョブは ABORTED になる）
- GET /api/stage/limits は XYZStage.limits を返す。Moonraker 不通と printer.cfg の不備は 502
"""

import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcbasm.hal.fake import FAKE_PRINTER_CONFIG, FakeAudioPlayer, FakeKlipper
from tests.web.api.jobs.conftest import register_gated
from tests.web.update_support import UpdateSandbox, make_update_sandbox
from web.api.app import create_app
from web.api.env import Env
from web.api.state import AppState
from web.selfupdate.runner import UpdateRunner


class TestKlipperStatus:
    """GET /api/klipper/status."""

    @pytest.mark.usefixtures("unreachable_klipper")
    def test_unreachable_moonraker_returns_200_with_connected_false(
        self, client: TestClient
    ):
        response = client.get("/api/klipper/status")

        assert response.status_code == 200
        data = response.json()
        assert data["connected"] is False
        assert data["error"]
        assert data["position"] is None


@pytest.mark.usefixtures("unreachable_klipper")
class TestEmergencyStop:
    """POST /api/emergency-stop."""

    def test_unreachable_moonraker_returns_502(self, client: TestClient):
        response = client.post("/api/emergency-stop")

        assert response.status_code == 502

    def test_aborts_running_job_even_when_moonraker_unreachable(
        self, client: TestClient, app: FastAPI
    ):
        # Phase 3: E-STOP の先頭で jobs.request_abort()（Klipper 送信失敗でも
        # abort フラグは立つ）
        register_gated(app.state.catalog, name="estop_target")
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
    """POST /api/firmware-restart."""

    def test_unreachable_moonraker_returns_502(self, client: TestClient):
        response = client.post("/api/firmware-restart")

        assert response.status_code == 502

    def test_bypasses_machine_lock(self, client: TestClient, appstate: AppState):
        # firmware restart はロック非経由: busy 中でも 409 にはならない
        # （Moonraker 不達のため 502 に到達する = ロックで弾かれていない）
        with appstate.machine_lock("pytest-job"):
            response = client.post("/api/firmware-restart")

        assert response.status_code == 502

    def test_aborts_running_job_even_when_moonraker_unreachable(
        self, client: TestClient, app: FastAPI
    ):
        # firmware restart の先頭で jobs.request_abort()。
        # Klipper 送信が 502 でも実行中ジョブは abort へ進む。
        register_gated(app.state.catalog, name="firmware_restart_target")
        response = client.post("/api/jobs/firmware_restart_target", json={})
        assert response.status_code == 201

        assert client.post("/api/firmware-restart").status_code == 502

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            job = client.get("/api/jobs/current").json()["job"]
            if job is not None and job["status"] == "aborted":
                return
            time.sleep(0.02)
        pytest.fail("firmware restart 後にジョブが aborted になりませんでした")


class TestStageLimits:
    """GET /api/stage/limits（Phase 3）."""

    @pytest.mark.usefixtures("unreachable_klipper")
    def test_unreachable_moonraker_returns_502(self, client: TestClient):
        response = client.get("/api/stage/limits")

        assert response.status_code == 502

    def test_missing_printer_cfg_section_returns_502_with_the_reason(
        self, client: TestClient, mount_klipper: Callable[[FakeKlipper], None]
    ):
        config = {k: v for k, v in FAKE_PRINTER_CONFIG.items() if k != "printer"}
        mount_klipper(FakeKlipper(config))

        response = client.get("/api/stage/limits")

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
            audio_player=FakeAudioPlayer(),
            paste_test_board_footprint_root=paste_test_board_footprint_root,
            update_runner=UpdateRunner(sandbox.settings),
        )
        with TestClient(app) as test_client:
            yield test_client

    def test_unreachable_moonraker_leaves_the_services_running(
        self, restart_client: TestClient, sandbox: UpdateSandbox
    ):
        assert restart_client.post("/api/firmware-restart").status_code == 502

        assert sandbox.restarts() == []
