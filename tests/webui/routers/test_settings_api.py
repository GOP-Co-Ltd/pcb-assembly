"""`webui.routers.settings_api` の仕様テスト.

計画書「routers」節 + spec §8:

- GET /api/settings/machine がホワイトリスト全項目（label / unit / value）を返す
- PUT は実ファイルへ反映しコメントを保持する
- 未知キー → 400、busy → 409

Phase 2 追記（計画書 webui-phase2.md「既存ルーターへの変更」節 + spec §8）:

- PUT /api/settings/machine で camera.* キーを書いたら FrameHub を再構築する

計画書 memory/agents/implementation-planner/webui-camera-calib.md「設計判断 b」
「公開インターフェース案 3」が追記契約:

- camera.crop.* は再構築条件から除外する（レンダラがフレーム毎に読むため
  デバイス再構築不要。crop 変更でストリームを切断しない）
- camera.crop.* は 1 以上の int（要確認事項 2 採用。0 / 負値 → 400）
"""

from pathlib import Path

import pytest
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

        assert fields["audio.device"]["value"] == "null"
        assert fields["audio.device"]["value_type"] == "str"
        assert fields["audio.volume"]["value"] == 1.0
        assert fields["audio.volume"]["value_type"] == "float"
        max_fill_speed = fields["paste_dispenser.max_fill_speed"]
        assert max_fill_speed["value"] == 0.8
        assert max_fill_speed["value_type"] == "float"
        assert max_fill_speed["label"]
        assert fields["paste_dispenser.dispense_mode"]["value"] == "auto"
        assert fields["paste_dispenser.dispense_mode"]["value_type"] == "dispense_mode"
        assert fields["paste_dispenser.auto_line_aspect_ratio"]["value"] == 1.618
        assert fields["paste_dispenser.paste_height"]["value"] == "auto"
        assert fields["paste_dispenser.paste_height"]["value_type"] == "float_or_auto"
        assert fields["paste_dispenser.lift_height"]["value"] == 2.0
        assert fields["paste_dispenser.lift_height"]["value_type"] == "float"
        assert fields["probe.lift_height"]["value_type"] == "float"
        assert fields["probe.board_edge_margin"]["value"] == 2.5
        assert fields["probe.board_edge_margin"]["value_type"] == "float"

    def test_get_reports_none_for_missing_keys(self, client: TestClient):
        fields = {
            field["key"]: field
            for field in client.get("/api/settings/machine").json()["fields"]
        }

        assert fields["paste_dispenser.bead_width_factor"]["value"] is None
        assert fields["probe.lift_height"]["value"] is None

    def test_put_writes_file_and_preserves_comments(
        self, client: TestClient, configs_root: Path
    ):
        path = configs_root / "kurousagi" / "machine.toml"
        before = path.read_text(encoding="utf-8").splitlines()

        response = client.put(
            "/api/settings/machine",
            json={
                "values": {
                    "paste_dispenser.rotations_per_ul": 9.876543,
                    "paste_dispenser.max_fill_speed": 0.9,
                    "probe.min_radius": 2.5,
                    "probe.board_edge_margin": 3.0,
                    "probe.min_samples": 7,
                }
            },
        )

        assert response.status_code == 200
        fields = {field["key"]: field for field in response.json()["fields"]}
        assert fields["paste_dispenser.rotations_per_ul"]["value"] == 9.876543
        assert fields["paste_dispenser.max_fill_speed"]["value"] == 0.9
        assert fields["probe.min_radius"]["value"] == 2.5
        assert fields["probe.board_edge_margin"]["value"] == 3.0
        assert fields["probe.min_samples"]["value"] == 7

        after_text = path.read_text(encoding="utf-8")
        after = after_text.splitlines()
        assert len(after) == len(before)
        changed = [(b, a) for b, a in zip(before, after) if b != a]
        assert len(changed) == 5
        # 変更対象外のコメントが無傷で残る
        assert "キャリブレーション値 2026/06/08" in after_text

    def test_put_writes_audio_settings(self, client: TestClient):
        response = client.put(
            "/api/settings/machine",
            json={
                "values": {
                    "audio.device": "plughw:CARD=Audio,DEV=0",
                    "audio.volume": 0.4,
                }
            },
        )

        assert response.status_code == 200, response.text
        fields = {field["key"]: field for field in response.json()["fields"]}
        assert fields["audio.device"]["value"] == "plughw:CARD=Audio,DEV=0"
        assert fields["audio.volume"]["value"] == 0.4

    @pytest.mark.parametrize(
        ("key", "value"),
        [("audio.device", " "), ("audio.volume", -0.1), ("audio.volume", 1.1)],
    )
    def test_put_invalid_audio_setting_returns_400(
        self, client: TestClient, key: str, value: str | float
    ):
        response = client.put("/api/settings/machine", json={"values": {key: value}})

        assert response.status_code == 400
        assert key in response.text

    def test_put_writes_dispense_mode_and_auto_height(self, client: TestClient):
        response = client.put(
            "/api/settings/machine",
            json={
                "values": {
                    "paste_dispenser.dispense_mode": "line",
                    "paste_dispenser.paste_height": "auto",
                    "paste_dispenser.auto_line_aspect_ratio": 1.7,
                }
            },
        )

        assert response.status_code == 200, response.text
        fields = {field["key"]: field for field in response.json()["fields"]}
        assert fields["paste_dispenser.dispense_mode"]["value"] == "line"
        assert fields["paste_dispenser.paste_height"]["value"] == "auto"
        assert fields["paste_dispenser.auto_line_aspect_ratio"]["value"] == 1.7

    def test_get_returns_reference_point_offset_pairs(self, client: TestClient):
        fields = {
            field["key"]: field
            for field in client.get("/api/settings/machine").json()["fields"]
        }

        top_left = fields["reference_point.offsets.top_left"]
        assert top_left["value"] == [5.0, -5.0]
        assert top_left["value_type"] == "float_pair"
        assert fields["reference_point.offsets.bottom_right"]["value"] is None

    def test_put_writes_float_pair(self, client: TestClient):
        response = client.put(
            "/api/settings/machine",
            json={"values": {"reference_point.offsets.bottom_right": [-5.0, 5.0]}},
        )

        assert response.status_code == 200, response.text
        fields = {field["key"]: field for field in response.json()["fields"]}
        assert fields["reference_point.offsets.bottom_right"]["value"] == [-5.0, 5.0]

    def test_put_invalid_float_pair_returns_400(self, client: TestClient):
        response = client.put(
            "/api/settings/machine",
            json={"values": {"reference_point.offsets.top_left": [1.0]}},
        )

        assert response.status_code == 400

    def test_put_unknown_key_returns_400(self, client: TestClient):
        response = client.put(
            "/api/settings/machine",
            json={"values": {"paste_dispenser.no_such_key": 1.0}},
        )

        assert response.status_code == 400

    def test_put_unknown_dispense_mode_returns_400(self, client: TestClient):
        response = client.put(
            "/api/settings/machine",
            json={"values": {"paste_dispenser.dispense_mode": "spray"}},
        )

        assert response.status_code == 400

    def test_put_invalid_auto_line_threshold_returns_400(self, client: TestClient):
        response = client.put(
            "/api/settings/machine",
            json={"values": {"paste_dispenser.auto_line_aspect_ratio": 1.0}},
        )

        assert response.status_code == 400

    def test_put_non_positive_board_edge_margin_returns_400(self, client: TestClient):
        response = client.put(
            "/api/settings/machine",
            json={"values": {"probe.board_edge_margin": 0.0}},
        )

        assert response.status_code == 400
        assert "board_edge_margin" in response.text

    def test_put_non_positive_paste_lift_height_returns_400(self, client: TestClient):
        response = client.put(
            "/api/settings/machine",
            json={"values": {"paste_dispenser.lift_height": 0.0}},
        )

        assert response.status_code == 400
        assert "lift_height" in response.text

    def test_put_while_busy_returns_409(self, client: TestClient, appstate: AppState):
        with appstate.machine_lock("pytest-job"):
            response = client.put(
                "/api/settings/machine",
                json={"values": {"paste_dispenser.max_fill_speed": 0.9}},
            )

        assert response.status_code == 409


class TestPadAlignMaxFailuresApi:
    """paste_dispenser.pad_align.max_failures の GET / PUT（paste-align-max-
    failures 計画書）."""

    def test_get_reports_none_with_int_type_when_missing(self, client: TestClient):
        fields = {
            field["key"]: field
            for field in client.get("/api/settings/machine").json()["fields"]
        }

        field = fields["paste_dispenser.pad_align.max_failures"]
        assert field["value"] is None
        assert field["value_type"] == "int"

    def test_put_writes_value(self, client: TestClient):
        response = client.put(
            "/api/settings/machine",
            json={"values": {"paste_dispenser.pad_align.max_failures": 2}},
        )

        assert response.status_code == 200, response.text
        fields = {field["key"]: field for field in response.json()["fields"]}
        assert fields["paste_dispenser.pad_align.max_failures"]["value"] == 2

    def test_put_negative_value_returns_400(self, client: TestClient):
        response = client.put(
            "/api/settings/machine",
            json={"values": {"paste_dispenser.pad_align.max_failures": -1}},
        )

        assert response.status_code == 400


class TestCameraSettingsRebuild:
    """camera.* キーの保存による FrameHub 再構築（Phase 2 + webui-camera-calib 計画書「設計判断
    b」）.

    camera.crop.* はレンダラがフレーム毎に読むため再構築対象から除外される （camera.fps 等の他 camera.*
    キーは従来どおり再構築する）。
    """

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
            json={"values": {"paste_dispenser.max_fill_speed": 0.9}},
        )

        assert response.status_code == 200
        assert fake_camera_appstate.frame_hub() is hub

    def test_put_camera_crop_key_keeps_frame_hub(
        self, fake_camera_client: TestClient, fake_camera_appstate: AppState
    ):
        """Crop は camera.* 前置だが再構築しない（ストリーム非切断の要件）."""
        hub = fake_camera_appstate.frame_hub()

        response = fake_camera_client.put(
            "/api/settings/machine",
            json={"values": {"camera.crop.width": 300, "camera.crop.height": 300}},
        )

        assert response.status_code == 200, response.text
        assert fake_camera_appstate.frame_hub() is hub

    def test_put_camera_crop_and_other_camera_key_rebuilds_frame_hub(
        self, fake_camera_client: TestClient, fake_camera_appstate: AppState
    ):
        """Camera.crop.* と他の camera.* キーが混在した PUT では再構築する（camera.crop.* 以外の
        camera.* が 1 つでも含まれていれば rebuild する境界のピン）."""
        hub = fake_camera_appstate.frame_hub()

        response = fake_camera_client.put(
            "/api/settings/machine",
            json={"values": {"camera.fps": 20.0, "camera.crop.width": 300}},
        )

        assert response.status_code == 200, response.text
        assert fake_camera_appstate.frame_hub() is not hub


class TestCameraCropValidation:
    """Camera.crop.* の 1 以上検証（webui-camera-calib 計画書・要確認事項 2）."""

    @pytest.mark.parametrize("key", ["camera.crop.width", "camera.crop.height"])
    @pytest.mark.parametrize("value", [0, -1])
    def test_put_non_positive_crop_returns_400(
        self, client: TestClient, key: str, value: int
    ):
        response = client.put("/api/settings/machine", json={"values": {key: value}})

        assert response.status_code == 400
