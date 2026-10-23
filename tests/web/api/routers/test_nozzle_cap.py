"""`web.api.routers.nozzle_cap` の仕様テスト.

計画書 memory/agents/implementation-planner/nozzle-cap-parking.md
「src/webui/routers/nozzle_cap.py」節 + 「API 契約」節が契約:

- `POST /api/pasting/nozzle-cap/record`
  - 200: 現在位置を 3 桁丸めで machine.toml に永続化し `{"x","y","z"}` を返す
  - 400: 全軸ホーミング済みでない（M84 後の stale 座標記録防止）
  - 409: machine_lock 取得失敗（detail に owner）
  - 502: Moonraker 不達（テスト用 config の port 7126 への実接続で検証、モック不使用）

`POST /api/pasting/nozzle-clean/test` は記録ではなく装置を動かすので、上に加えて
「位置が未記録なら Klipper に触れる前に 400」「可動域外ならプライムも含めて何も送らずに 400」を契約に持つ。

実機系（記録の永続化・relax 後の 400・実クリーニング動作）は受け入れ試験（docs/acceptance.md の A2）で確かめる。
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pcbasm.hal.fake import FakeKlipper
from web.api.settings import Settings
from web.api.state import AppState


class TestRecordNozzleCap:
    """ノズルキャップ位置の記録エンドポイント."""

    @pytest.mark.usefixtures("unreachable_klipper")
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


class TestRecordNozzleClean:
    """ノズルクリーニング位置の記録エンドポイント（キャップと同じ契約）.

    キャップと違い、表示文字列 `label` も返す（JS に整形を持たせないため）。
    """

    @pytest.mark.usefixtures("unreachable_klipper")
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

    @pytest.mark.usefixtures("unreachable_klipper")
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

    def test_out_of_reach_returns_400_without_sending(
        self, config_dir: Path, machine_klipper: FakeKlipper, client: TestClient
    ):
        """クリーニング G-code を組む段で可動域外と分かるので、プライムも送らない."""
        machine_klipper.set_status("toolhead", "homed_axes", "xyz")
        path = config_dir / "machine.toml"
        with path.open("a", encoding="utf-8") as machine_toml:
            machine_toml.write(
                "\n[paste_dispenser.nozzle_clean]\nx = 299.0\ny = 20.0\nz = -3.0\n"
            )

        response = client.post("/api/pasting/nozzle-clean/test")

        assert response.status_code == 400
        assert "可動域外" in response.json()["detail"]
        assert machine_klipper.sent == ()

    def test_busy_returns_409_with_owner_in_detail(
        self, client: TestClient, appstate: AppState
    ):
        """ジョブ実行中は「未記録」より先に「使用中」を返す（装置排他が優先）."""
        with appstate.machine_lock("pytest-job"):
            response = client.post("/api/pasting/nozzle-clean/test")

        assert response.status_code == 409
        assert "pytest-job" in response.text
