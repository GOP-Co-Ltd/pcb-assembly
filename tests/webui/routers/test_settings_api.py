"""`webui.routers.settings_api` の仕様テスト.

計画書「routers」節 + spec §8:

- GET /api/settings/machine|motion がホワイトリスト全項目（label / unit / value）を返す
- PUT は実ファイルへ反映しコメントを保持する
- motion GET は symlink_ok を返す
- restart=True で Moonraker 不達（port 7126）でも保存成功なら 200 + restart_ok=false
- 未知キー → 400、busy → 409
"""

from pathlib import Path

from fastapi.testclient import TestClient

from webui.config_store import MACHINE_FIELDS, MOTION_FIELDS
from webui.settings import Settings
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
                }
            },
        )

        assert response.status_code == 200
        fields = {field["key"]: field for field in response.json()["fields"]}
        assert fields["paste_dispenser.fill_speed"]["value"] == 0.9
        assert fields["probe.down_distance"]["value"] == 2.5

        after_text = path.read_text(encoding="utf-8")
        after = after_text.splitlines()
        assert len(after) == len(before)
        changed = [(b, a) for b, a in zip(before, after) if b != a]
        assert len(changed) == 2
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


class TestMotionSettingsApi:
    """GET / PUT /api/settings/motion."""

    def test_get_returns_motion_fields_and_symlink_flag(self, client: TestClient):
        response = client.get("/api/settings/motion")

        assert response.status_code == 200
        data = response.json()
        assert data["machine"] == "kurousagi"
        fields = {field["key"]: field for field in data["fields"]}
        assert set(fields) == {spec.key for spec in MOTION_FIELDS}
        assert fields["printer.max_velocity"]["value"] == 50.0
        # printer_cfg_link は存在しない → 選択マシンを指していない
        assert data["symlink_ok"] is False

    def test_symlink_ok_true_when_link_targets_selected_machine(
        self, client: TestClient, webui_settings: Settings, configs_root: Path
    ):
        link = webui_settings.printer_cfg_link
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(configs_root / "kurousagi" / "printer.cfg")

        assert client.get("/api/settings/motion").json()["symlink_ok"] is True

    def test_put_without_restart_updates_file(self, client: TestClient):
        response = client.put(
            "/api/settings/motion",
            json={"values": {"printer.max_velocity": 45.0}},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["restart_requested"] is False

        fields = {
            field["key"]: field
            for field in client.get("/api/settings/motion").json()["fields"]
        }
        assert fields["printer.max_velocity"]["value"] == 45.0

    def test_put_with_restart_reports_failure_when_moonraker_unreachable(
        self, client: TestClient
    ):
        # test-fixture（port 7126 = 非リッスン）を選択して RESTART 不達パスを実接続で検証
        assert (
            client.put("/api/machine", json={"name": "test-fixture"}).status_code == 200
        )

        response = client.put(
            "/api/settings/motion",
            json={"values": {"printer.max_accel": 450.0}, "restart": True},
        )

        # 保存と再起動は別の関心事: 保存成功なら 200
        assert response.status_code == 200
        data = response.json()
        assert data["restart_requested"] is True
        assert data["restart_ok"] is False
        assert data["restart_error"]

        fields = {
            field["key"]: field
            for field in client.get("/api/settings/motion").json()["fields"]
        }
        assert fields["printer.max_accel"]["value"] == 450.0

    def test_put_unknown_key_returns_400(self, client: TestClient):
        response = client.put(
            "/api/settings/motion",
            json={"values": {"printer.no_such_option": 1.0}},
        )

        assert response.status_code == 400

    def test_put_while_busy_returns_409(self, client: TestClient, appstate: AppState):
        with appstate.machine_lock("pytest-job"):
            response = client.put(
                "/api/settings/motion",
                json={"values": {"printer.max_velocity": 45.0}},
            )

        assert response.status_code == 409
