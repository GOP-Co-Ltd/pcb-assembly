"""`web.api.routers.nozzle_cap` の仕様テスト.

計画書 memory/agents/implementation-planner/nozzle-cap-parking.md
「src/webui/routers/nozzle_cap.py」節 + 「API 契約」節が契約:

- `POST /api/pasting/nozzle-cap/record`
  - 200: 現在位置を 3 桁丸めで machine.toml に永続化し `{"x","y","z"}` を返す
  - 400: 全軸ホーミング済みでない（M84 後の stale 座標記録防止）
  - 409: machine_lock 取得失敗（detail に owner）
  - 502: Moonraker 不達（テスト用 config の port 7126 への実接続で検証、モック不使用）

`POST /api/pasting/nozzle-clean/test` は記録ではなく装置を動かすので、上に加えて
「位置が未記録なら Klipper に触れる前に 400」を契約に持つ。

実機系（記録永続化・relax 後 400・実クリーニング動作）は `@mark_hardware`（ユーザー実行）。
"""

from pathlib import Path

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
        assert "[paste_dispenser.nozzle_cap]" in machine_toml

        # M84 で homed 状態が消えた後は stale 座標を記録させない
        relax = real_client.post("/api/machine-control", json={"action": "relax"})
        assert relax.status_code == 200
        stale = real_client.post("/api/pasting/nozzle-cap/record")
        assert stale.status_code == 400
        assert "ホーミング" in stale.text


class TestRecordNozzleClean:
    """ノズルクリーニング位置の記録エンドポイント（キャップと同じ契約）.

    キャップと違い、表示文字列 `label` も返す（JS に整形を持たせないため）。
    """

    def test_unreachable_moonraker_returns_502(self, client: TestClient):
        response = client.post("/api/pasting/nozzle-clean/record")

        assert response.status_code == 502

    def test_busy_returns_409_with_owner_in_detail(
        self, client: TestClient, appstate: AppState
    ):
        with appstate.machine_lock("pytest-job"):
            response = client.post("/api/pasting/nozzle-clean/record")

        assert response.status_code == 409
        assert "pytest-job" in response.text

    @mark_hardware
    def test_record_persists_position_and_returns_label(
        self, real_client: TestClient, real_settings: Settings
    ):
        home = real_client.post("/api/machine-control", json={"action": "home"})
        assert home.status_code == 200

        response = real_client.post("/api/pasting/nozzle-clean/record")

        assert response.status_code == 200
        recorded = response.json()
        assert set(recorded) == {"x", "y", "z", "label"}
        machine_toml = (real_settings.config_dir / "machine.toml").read_text(
            encoding="utf-8"
        )
        assert "[paste_dispenser.nozzle_clean]" in machine_toml


class TestNozzleCleanTestRun:
    """クリーニング動作のテスト実行エンドポイント.

    記録と違って装置を動かすので、検査の順序そのものが契約になる。未記録は Klipper に
    触れる前に 400 で断り（`create_klipper` は接続しないので 502 に化けない）、記録済み
    なら homed_axes の取得まで進んでテスト用 config の Klipper（port 7126 非リッスン）で
    502 になる = 位置の検証を通過した証明。
    """

    def test_unrecorded_clean_returns_400(self, client: TestClient):
        response = client.post("/api/pasting/nozzle-clean/test")

        assert response.status_code == 400
        assert response.json()["detail"] == "ノズルクリーニング位置が未記録です"

    def test_partially_recorded_clean_returns_400(
        self, partial_nozzle_clean: Path, client: TestClient
    ):
        """動作値だけ保存された座標無しのテーブルは「未記録」として断る."""
        response = client.post("/api/pasting/nozzle-clean/test")

        assert response.status_code == 400
        assert response.json()["detail"] == "ノズルクリーニング位置が未記録です"

    def test_recorded_clean_passes_validation_and_returns_502(
        self, config_dir: Path, client: TestClient
    ):
        path = config_dir / "machine.toml"
        with path.open("a", encoding="utf-8") as machine_toml:
            machine_toml.write(
                "\n[paste_dispenser.nozzle_clean]\n"
                "x = 10.0\ny = 20.0\nz = -30.0\npress_depth = 0.4\n"
            )

        response = client.post("/api/pasting/nozzle-clean/test")

        assert response.status_code == 502

    def test_busy_returns_409_with_owner_in_detail(
        self, client: TestClient, appstate: AppState
    ):
        """ジョブ実行中は「未記録」より先に「使用中」を返す（装置排他が優先）."""
        with appstate.machine_lock("pytest-job"):
            response = client.post("/api/pasting/nozzle-clean/test")

        assert response.status_code == 409
        assert "pytest-job" in response.text

    @mark_hardware
    def test_runs_cleaning_and_returns_summary(self, real_client: TestClient):
        """実機でクリーニング動作を通し、実施内容の 1 行を返す."""
        home = real_client.post("/api/machine-control", json={"action": "home"})
        assert home.status_code == 200

        response = real_client.post("/api/pasting/nozzle-clean/test")

        assert response.status_code == 200
        assert "ノズルクリーニング" in response.json()["message"]

    @mark_hardware
    def test_stale_after_relax_returns_400(self, real_client: TestClient):
        """M84 後は未ホーミングなので、絶対移動を送る前に 400 で断る."""
        relax = real_client.post("/api/machine-control", json={"action": "relax"})
        assert relax.status_code == 200

        response = real_client.post("/api/pasting/nozzle-clean/test")

        assert response.status_code == 400
        assert "ホーミング" in response.text
