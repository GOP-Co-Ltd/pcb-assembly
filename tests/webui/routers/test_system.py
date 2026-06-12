"""`webui.routers.system` の仕様テスト.

計画書「routers」節:

- GET /api/klipper/status は常に 200。接続失敗は connected=false + error
- POST /api/emergency-stop は接続不能で 502、ロック非経由
- 実 Moonraker への status は `@mark_hardware`（ユーザー実行）。
  emergency_stop の実機テストは書かない（装置への副作用が大きい）
"""

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
