"""`web.api.routers.nozzle_cap` の仕様テスト.

計画書 memory/agents/implementation-planner/nozzle-cap-parking.md
「src/webui/routers/nozzle_cap.py」節 + 「API 契約」節が契約:

- `POST /api/pasting/nozzle-cap/record`
  - 200: 現在位置を 3 桁丸めで machine.toml に永続化し `{"x","y","z"}` を返す
  - 400: 全軸ホーミング済みでない（M84 後の stale 座標記録防止）
  - 409: machine_lock 取得失敗（detail に owner）
  - 502: Moonraker 不達（テスト用 config の port 7126 への実接続で検証、モック不使用）

実機系（記録永続化・relax 後 400）は `@mark_hardware`（ユーザー実行）。
"""

from fastapi.testclient import TestClient

from tests.helpers import mark_hardware
from web.api.settings import Settings
from web.api.state import AppState


class TestRecordNozzleCap:
    """ノズルキャップ位置の記録エンドポイント."""

    def test_unreachable_moonraker_returns_502(self, client: TestClient):
        """Klipper 不達（port 7126）では書き込み前に 502 で終わる."""
        response = client.post("/api/pasting/nozzle-cap/record")

        assert response.status_code == 502

    def test_busy_returns_409_with_owner_in_detail(
        self, client: TestClient, appstate: AppState
    ):
        with appstate.machine_lock("pytest-job"):
            response = client.post("/api/pasting/nozzle-cap/record")

        assert response.status_code == 409
        assert "pytest-job" in response.text

    @mark_hardware
    def test_record_persists_position_and_stale_after_relax_returns_400(
        self, real_client: TestClient, real_settings: Settings
    ):
        """全軸ホーミング後の記録は永続化され、relax（M84）後の記録は 400 になる."""
        home = real_client.post("/api/machine-control", json={"action": "home"})
        assert home.status_code == 200
        assert home.json()["homed_axes"] == "xyz"

        response = real_client.post("/api/pasting/nozzle-cap/record")

        assert response.status_code == 200
        recorded = response.json()
        assert set(recorded) == {"x", "y", "z"}
        machine_toml = (real_settings.config_dir / "machine.toml").read_text(
            encoding="utf-8"
        )
        assert "[nozzle_cap]" in machine_toml

        # M84 で homed 状態が消えた後は stale 座標を記録させない
        relax = real_client.post("/api/machine-control", json={"action": "relax"})
        assert relax.status_code == 200
        stale = real_client.post("/api/pasting/nozzle-cap/record")
        assert stale.status_code == 400
        assert "ホーミング" in stale.text
