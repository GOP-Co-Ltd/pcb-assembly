"""`webui.routers.system` の仕様テスト.

計画書「routers」節:

- GET /api/klipper/status は常に 200。接続失敗は connected=false + error
- POST /api/emergency-stop は接続不能で 502、ロック非経由
- POST /api/firmware-restart は接続不能で 502、ロック非経由、
  先頭で jobs.request_abort()
- 実 Moonraker への status は `@mark_hardware`（ユーザー実行）。
  emergency_stop / firmware_restart の実機テストは書かない
  （装置への副作用が大きい）

Phase 3 追記（計画書 webui-phase3.md「既存ルーター・app への変更」節）:

- E-STOP は先頭で jobs.request_abort() を呼ぶ（Moonraker 不達で 502 でも
  実行中ジョブは ABORTED になる）
- GET /api/stage/limits は XYZStage.limits を返す。Moonraker 不通は 502
"""

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.helpers import mark_hardware
from webui.state import AppState


class TestKlipperStatus:
    """GET /api/klipper/status."""

    def test_unreachable_moonraker_returns_200_with_connected_false(
        self, client: TestClient
    ):
        response = client.get("/api/klipper/status")

        assert response.status_code == 200
        data = response.json()
        assert data["connected"] is False
        assert data["error"]
        assert data["position"] is None

    @mark_hardware
    def test_real_moonraker_reports_position_and_homed_axes(
        self, real_client: TestClient
    ):
        response = real_client.get("/api/klipper/status")

        assert response.status_code == 200
        data = response.json()
        assert data["connected"] is True
        assert data["error"] is None
        position = data["position"]
        assert isinstance(position["x"], float)
        assert isinstance(position["y"], float)
        assert isinstance(position["z"], float)
        assert isinstance(data["homed_axes"], str)


class TestEmergencyStop:
    """POST /api/emergency-stop."""

    def test_unreachable_moonraker_returns_502(self, client: TestClient):
        response = client.post("/api/emergency-stop")

        assert response.status_code == 502

    def test_bypasses_machine_lock(self, client: TestClient, appstate: AppState):
        # E-STOP はロック非経由: busy 中でも 409 にはならない
        # （Moonraker 不達のため 502 に到達する = ロックで弾かれていない）
        with appstate.machine_lock("pytest-job"):
            response = client.post("/api/emergency-stop")

        assert response.status_code == 502

    def test_aborts_running_job_even_when_moonraker_unreachable(
        self, client: TestClient, app: FastAPI
    ):
        # Phase 3: E-STOP の先頭で jobs.request_abort()（Klipper 送信失敗でも
        # abort フラグは立つ）
        from webui.jobs.catalog import JobDefinition
        from webui.jobs.context import JobContext

        def run(ctx: JobContext) -> None:
            while True:
                ctx.checkpoint()
                time.sleep(0.01)

        app.state.catalog.register(
            JobDefinition(
                name="estop_target",
                label="E-STOP 検証ジョブ",
                tab="dev",
                run=run,
                uses_machine=False,
                hidden=True,
            )
        )
        assert client.post("/api/jobs/estop_target", json={}).status_code == 201

        assert client.post("/api/emergency-stop").status_code == 502

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            job = client.get("/api/jobs/current").json()["job"]
            if job is not None and job["status"] == "aborted":
                return
            time.sleep(0.02)
        pytest.fail("E-STOP 後にジョブが aborted になりませんでした")


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
        from webui.jobs.catalog import JobDefinition
        from webui.jobs.context import JobContext

        def run(ctx: JobContext) -> None:
            while True:
                ctx.checkpoint()
                time.sleep(0.01)

        app.state.catalog.register(
            JobDefinition(
                name="firmware_restart_target",
                label="firmware restart 検証ジョブ",
                tab="dev",
                run=run,
                uses_machine=False,
                hidden=True,
            )
        )
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

    def test_unreachable_moonraker_returns_502(self, client: TestClient):
        response = client.get("/api/stage/limits")

        assert response.status_code == 502

    @mark_hardware
    def test_real_stage_reports_min_max_per_axis(self, real_client: TestClient):
        response = real_client.get("/api/stage/limits")

        assert response.status_code == 200
        data = response.json()
        for axis in ("x", "y", "z"):
            assert data[axis]["min"] < data[axis]["max"]
