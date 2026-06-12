"""`webui.routers.machine` の仕様テスト.

計画書「routers」節:

- GET /api/state — StateResponse の全フィールド
- GET /api/machines / GET・PUT /api/machine
- 未知マシン PUT → 404、ロック保持中 → 409
"""

from fastapi.testclient import TestClient

from webui.settings import Settings
from webui.state import AppState


class TestStateApi:
    """GET /api/state."""

    def test_state_reports_all_fields(
        self, client: TestClient, webui_settings: Settings
    ):
        response = client.get("/api/state")

        assert response.status_code == 200
        data = response.json()
        assert data["machine"] == "kurousagi"
        assert data["pcb_file"] is None
        assert data["busy"] is False
        assert data["busy_owner"] is None
        assert data["focus_z"] == -25.0
        assert data["mainsail_url"] == webui_settings.mainsail_url

    def test_state_reports_busy_while_locked(
        self, client: TestClient, appstate: AppState
    ):
        with appstate.machine_lock("pytest-job"):
            data = client.get("/api/state").json()

        assert data["busy"] is True
        assert data["busy_owner"] == "pytest-job"


class TestMachineApi:
    """マシン一覧・選択."""

    def test_get_machines_lists_all_with_selection(self, client: TestClient):
        response = client.get("/api/machines")

        assert response.status_code == 200
        data = response.json()
        assert data["machines"] == ["kurousagi", "test-fixture"]
        assert data["selected"] == "kurousagi"

    def test_get_machine_returns_current_selection(self, client: TestClient):
        response = client.get("/api/machine")

        assert response.status_code == 200
        assert response.json() == {"name": "kurousagi"}

    def test_put_machine_switches_selection(self, client: TestClient):
        response = client.put("/api/machine", json={"name": "test-fixture"})

        assert response.status_code == 200
        assert response.json() == {"name": "test-fixture"}
        assert client.get("/api/state").json()["machine"] == "test-fixture"

    def test_put_unknown_machine_returns_404(self, client: TestClient):
        response = client.put("/api/machine", json={"name": "no-such-machine"})

        assert response.status_code == 404

    def test_put_machine_while_busy_returns_409(
        self, client: TestClient, appstate: AppState
    ):
        with appstate.machine_lock("pytest-job"):
            response = client.put("/api/machine", json={"name": "test-fixture"})

        assert response.status_code == 409
        assert "pytest-job" in response.text
