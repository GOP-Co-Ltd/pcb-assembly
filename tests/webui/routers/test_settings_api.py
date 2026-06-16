"""`webui.routers.settings_api` の仕様テスト.

計画書「routers」節 + spec §8:

- GET /api/settings/machine がホワイトリスト全項目（label / unit / value）を返す
- PUT は実ファイルへ反映しコメントを保持する
- 未知キー → 400、busy → 409

Phase 2 追記（計画書 webui-phase2.md「既存ルーターへの変更」節 + spec §8）:

- PUT /api/settings/machine で camera.* キーを書いたら FrameHub を再構築する
"""

from pathlib import Path

from fastapi.testclient import TestClient

from webui.config_store import MACHINE_FIELDS
from webui.state import AppState


class TestMachineSettingsApi:
    """GET / PUT /api/settings/machine."""

    def test_get_returns_every_whitelisted_field(self, client: TestClient):
        response = client.get("/api/settings/machine")

        assert response.status_code == 200
        data = response.json()
        assert data["machine"] == "kurousagi"
        fields = {field["key"]: field for field in data["fields"]}
        assert set(fields) == {spec.key for spec in MACHINE_FIELDS}

        fill_speed = fields["paste_dispenser.fill_speed"]
        assert fill_speed["value"] == 0.8
        assert fill_speed["value_type"] == "float"
        assert fill_speed["label"]
        assert fields["probe.shift"]["value"] == [-0.5, 0.0]
        assert fields["probe.shift"]["value_type"] == "float_pair"

    def test_get_reports_none_for_missing_keys(self, client: TestClient):
        fields = {
            field["key"]: field
            for field in client.get("/api/settings/machine").json()["fields"]
        }

        assert fields["paste_dispenser.bead_width_factor"]["value"] is None

    def test_put_writes_file_and_preserves_comments(
        self, client: TestClient, configs_root: Path
    ):
        path = configs_root / "kurousagi" / "machine.toml"
        before = path.read_text(encoding="utf-8").splitlines()

        response = client.put(
            "/api/settings/machine",
            json={
                "values": {
                    "paste_dispenser.fill_speed": 0.9,
                    "probe.down_distance": 2.5,
                    "probe.shift": [0.25, -0.75],
                }
            },
        )

        assert response.status_code == 200
        fields = {field["key"]: field for field in response.json()["fields"]}
        assert fields["paste_dispenser.fill_speed"]["value"] == 0.9
        assert fields["probe.down_distance"]["value"] == 2.5
        assert fields["probe.shift"]["value"] == [0.25, -0.75]

        after_text = path.read_text(encoding="utf-8")
        after = after_text.splitlines()
        assert len(after) == len(before)
        changed = [(b, a) for b, a in zip(before, after) if b != a]
        assert len(changed) == 3
        # 変更対象外のコメントが無傷で残る
        assert "キャリブレーション値 2026/06/08" in after_text

    def test_put_unknown_key_returns_400(self, client: TestClient):
        response = client.put(
            "/api/settings/machine",
            json={"values": {"paste_dispenser.no_such_key": 1.0}},
        )

        assert response.status_code == 400

    def test_put_while_busy_returns_409(self, client: TestClient, appstate: AppState):
        with appstate.machine_lock("pytest-job"):
            response = client.put(
                "/api/settings/machine",
                json={"values": {"paste_dispenser.fill_speed": 0.9}},
            )

        assert response.status_code == 409


class TestCameraSettingsRebuild:
    """camera.* キーの保存による FrameHub 再構築（Phase 2）."""

    def test_put_camera_key_rebuilds_frame_hub(
        self, fake_camera_client: TestClient, fake_camera_appstate: AppState
    ):
        hub = fake_camera_appstate.frame_hub()

        response = fake_camera_client.put(
            "/api/settings/machine",
            json={"values": {"camera.fps": 20.0}},
        )

        assert response.status_code == 200
        assert fake_camera_appstate.frame_hub() is not hub

    def test_put_without_camera_key_keeps_frame_hub(
        self, fake_camera_client: TestClient, fake_camera_appstate: AppState
    ):
        hub = fake_camera_appstate.frame_hub()

        response = fake_camera_client.put(
            "/api/settings/machine",
            json={"values": {"paste_dispenser.fill_speed": 0.9}},
        )

        assert response.status_code == 200
        assert fake_camera_appstate.frame_hub() is hub
