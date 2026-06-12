"""`webui.routers.machine_control` の仕様テスト.

計画書「routers」節 + spec §6 マシン操作パネル:

- 400: パラメータ不足（jog の axis/distance 欠落）/ focus_z 不可（z_position なし）/ limits 超過
- 409: machine_lock 取得失敗（detail に owner）
- 502: Moonraker 接続不能（test-fixture port 7126 への実接続で検証、モック不使用）
- 200: 実機（実 Moonraker）での操作完了 → `@mark_hardware`（ユーザー実行）
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.helpers import mark_hardware
from webui.state import AppState


class TestMachineControlValidation:
    """パラメータ検証（Moonraker 接続前に 400）."""

    def test_jog_without_distance_returns_400(self, client: TestClient):
        response = client.post(
            "/api/machine-control", json={"action": "jog", "axis": "x"}
        )

        assert response.status_code == 400

    def test_jog_without_axis_returns_400(self, client: TestClient):
        response = client.post(
            "/api/machine-control", json={"action": "jog", "distance": 1.0}
        )

        assert response.status_code == 400

    def test_focus_z_without_z_position_returns_400(
        self, client: TestClient, configs_root: Path
    ):
        calibration = configs_root / "kurousagi" / "ov9281_test_fixture.json"
        data = json.loads(calibration.read_text(encoding="utf-8"))
        data["z_position"] = None
        calibration.write_text(json.dumps(data), encoding="utf-8")

        response = client.post("/api/machine-control", json={"action": "focus_z"})

        assert response.status_code == 400


class TestMachineControlExclusion:
    """ジョブ共有ロックとの排他."""

    def test_busy_returns_409_with_owner_in_detail(
        self, client: TestClient, appstate: AppState
    ):
        with appstate.machine_lock("pytest-job"):
            response = client.post("/api/machine-control", json={"action": "relax"})

        assert response.status_code == 409
        assert "pytest-job" in response.text


class TestMachineControlMoonrakerDown:
    """Moonraker 不達（port 7126 = 非リッスン、実接続で検証）."""

    @pytest.mark.parametrize("action", ["home", "relax"])
    def test_unreachable_moonraker_returns_502(self, client: TestClient, action: str):
        response = client.post("/api/machine-control", json={"action": action})

        assert response.status_code == 502


class TestMachineControlHardware:
    """実 Moonraker（kurousagi）に対する操作。ユーザーが実行する."""

    @mark_hardware
    def test_relax_returns_status(self, real_client: TestClient):
        response = real_client.post("/api/machine-control", json={"action": "relax"})

        assert response.status_code == 200
        assert response.json()["connected"] is True

    @mark_hardware
    def test_jog_unhomed_returns_502(self, real_client: TestClient):
        # M84（relax）はステッパーを無効化し homed 状態をクリアする
        assert (
            real_client.post(
                "/api/machine-control", json={"action": "relax"}
            ).status_code
            == 200
        )

        response = real_client.post(
            "/api/machine-control",
            json={"action": "jog", "axis": "x", "distance": 0.1},
        )

        assert response.status_code == 502

    @mark_hardware
    def test_home_then_jog_round_trip(self, real_client: TestClient):
        home = real_client.post("/api/machine-control", json={"action": "home"})
        assert home.status_code == 200
        assert home.json()["homed_axes"] == "xyz"
        start_x = home.json()["position"]["x"]

        forward = real_client.post(
            "/api/machine-control",
            json={"action": "jog", "axis": "x", "distance": 0.1},
        )
        assert forward.status_code == 200
        assert forward.json()["position"]["x"] == pytest.approx(start_x + 0.1, abs=0.01)

        back = real_client.post(
            "/api/machine-control",
            json={"action": "jog", "axis": "x", "distance": -0.1},
        )
        assert back.status_code == 200
        assert back.json()["position"]["x"] == pytest.approx(start_x, abs=0.01)

    @mark_hardware
    def test_move_beyond_limits_returns_400(self, real_client: TestClient):
        assert (
            real_client.post(
                "/api/machine-control", json={"action": "home"}
            ).status_code
            == 200
        )

        response = real_client.post(
            "/api/machine-control", json={"action": "move", "x": 9999.0}
        )

        assert response.status_code == 400
