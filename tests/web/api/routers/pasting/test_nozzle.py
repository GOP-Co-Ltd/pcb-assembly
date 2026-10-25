"""`web.api.routers.pasting.nozzle` の仕様テスト.

計画書 memory/agents/implementation-planner/nozzle-cap-parking.md
「src/webui/routers/nozzle_cap.py」節 + 「API 契約」節が契約:

- `POST /api/pasting/nozzle/cap/record`
  - 200: 現在位置を 3 桁丸めで machine.toml に永続化し `{"x","y","z","label"}` を返す（`label` はサーバーが組む表示文字列）
  - 400: 全軸ホーミング済みでない（M84 後の stale 座標記録防止）
  - 409: machine_lock 取得失敗（detail に owner）
  - 502: Moonraker 不達（テスト用 config の port 7126 への実接続で検証、モック不使用）

実機系（記録の永続化・relax 後の 400）は受け入れ試験（docs/acceptance.md の A2）で確かめる。
"""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from pcbasm.hal.fake import FakeKlipper
from web.api.backend import Backend


@pytest.fixture
def homed_at(mount_klipper: Callable[[FakeKlipper], None]) -> FakeKlipper:
    """全軸ホーミング済みで (10.0, 20.0, -3.5) にいる fake 機体."""
    klipper = FakeKlipper()
    klipper.set_status("toolhead", "homed_axes", "xyz")
    klipper.set_status("gcode_move", "gcode_position", [10.0, 20.0, -3.5, 0.0])
    mount_klipper(klipper)
    return klipper


class TestRecordNozzleCap:
    """ノズルキャップ位置の記録エンドポイント."""

    @pytest.mark.usefixtures("homed_at")
    def test_returns_the_recorded_position_with_its_label(self, client: TestClient):
        response = client.post("/api/pasting/nozzle/cap/record")

        assert response.status_code == 200
        assert response.json() == {
            "x": 10.0,
            "y": 20.0,
            "z": -3.5,
            "label": "X10.000 Y20.000 Z-3.500",
        }

    @pytest.mark.usefixtures("unreachable_klipper")
    def test_unreachable_moonraker_returns_502(self, client: TestClient):
        """Klipper 不達（port 7126）では書き込み前に 502 で終わる."""
        response = client.post("/api/pasting/nozzle/cap/record")

        assert response.status_code == 502

    def test_busy_returns_409_with_owner_in_detail(
        self, client: TestClient, backend: Backend
    ):
        with backend.lock.hold("pytest-job"):
            response = client.post("/api/pasting/nozzle/cap/record")

        assert response.status_code == 409
        assert "pytest-job" in response.text


class TestRecordNozzleClean:
    """ノズルクリーニング位置の記録エンドポイント（キャップと同じ契約）."""

    @pytest.mark.usefixtures("homed_at")
    def test_returns_the_recorded_position_with_its_label(self, client: TestClient):
        response = client.post("/api/pasting/nozzle/clean/record")

        assert response.status_code == 200
        assert response.json()["label"] == "(10.00, 20.00, -3.50) mm"

    @pytest.mark.usefixtures("unreachable_klipper")
    def test_unreachable_moonraker_returns_502(self, client: TestClient):
        response = client.post("/api/pasting/nozzle/clean/record")

        assert response.status_code == 502

    def test_busy_returns_409_with_owner_in_detail(
        self, client: TestClient, backend: Backend
    ):
        with backend.lock.hold("pytest-job"):
            response = client.post("/api/pasting/nozzle/clean/record")

        assert response.status_code == 409
        assert "pytest-job" in response.text
