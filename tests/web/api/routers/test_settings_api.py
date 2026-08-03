"""`web.api.routers.settings_api` の仕様テスト.

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
from typing import override

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.api.config_store import MACHINE_FIELDS
from web.api.discovery import ServiceAdvertiser
from web.api.state import AppState


class RecordingAdvertiser(ServiceAdvertiser):
    """`update` の呼び出しだけを記録する ServiceAdvertiser（ソケットは開かない）.

    実 `ServiceAdvertiser` を継承するのは、広告の再登録を観測する手段が
    ``update`` の呼び出しそのものしか無く（結果はマルチキャストの先にある）、
    かつシグネチャを型で縛ったままにしたいため。
    """

    def __init__(self) -> None:
        super().__init__(
            machine_id="recording",
            port=8081,
            name=None,
            machine_type=None,
            addresses=(),
        )
        self.updates: list[str | None] = []

    @override
    def update(self, name: str | None) -> None:
        self.updates.append(name)


class TestMachineSettingsApi:
    """GET / PUT /api/settings/machine."""

    def test_get_returns_every_whitelisted_field(self, client: TestClient):
        response = client.get("/api/settings/machine")

        assert response.status_code == 200
        data = response.json()
        fields = {field["key"]: field for field in data["fields"]}
        assert set(fields) == {spec.key for spec in MACHINE_FIELDS}

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

    def test_get_resolves_defaults_for_missing_keys(self, client: TestClient):
        """未記載キーは `value` が None でも `resolved` に実効値（既定値）が入る（MR4）.

        SSR ページはこの実効値で現在値を描く。frontend 側で 0 などに代替すると、その値が 「設定に保存」で
        machine.toml へ書き戻されて装置の挙動を壊す。
        """
        fields = {
            field["key"]: field
            for field in client.get("/api/settings/machine").json()["fields"]
        }

        assert fields["paste_dispenser.bead_width_factor"]["resolved"] == 1.0
        assert fields["probe.lift_height"]["resolved"] == 1.0
        # 記載があるキーは書かれている値がそのまま実効値
        assert fields["paste_dispenser.max_fill_speed"]["resolved"] == 0.8
        assert fields["paste_dispenser.paste_height"]["resolved"] == "auto"
        assert fields["reference_point.offsets.top_left"]["resolved"] == [5.0, -5.0]

    def test_get_resolves_each_section_independently(
        self, client: TestClient, config_dir: Path
    ):
        """必須キーを欠いたセクションだけが `resolved` を失う（他セクションは残る）.

        `[probe]` の `min_radius` は既定値を持たないので、消すと cattrs が `Probe` を
        組めない。1 セクションの不備で全項目の実効値を失うと、無関係なページまで
        現在値を描けなくなる。
        """
        path = config_dir / "machine.toml"
        path.write_text(
            "".join(
                line
                for line in path.read_text(encoding="utf-8").splitlines(keepends=True)
                if not line.startswith("min_radius")
            ),
            encoding="utf-8",
        )

        fields = {
            field["key"]: field
            for field in client.get("/api/settings/machine").json()["fields"]
        }

        assert fields["probe.min_samples"]["resolved"] is None
        assert fields["probe.min_samples"]["value"] == 6
        assert fields["paste_dispenser.pad_align.canny_low"]["resolved"] == 81.0

    def test_put_writes_file_and_preserves_comments(
        self, client: TestClient, config_dir: Path
    ):
        path = config_dir / "machine.toml"
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


class TestAdvertisementUpdate:
    """PUT で表示名が変わったときだけ mDNS 広告を更新する（実装契約 §4）.

    無条件に呼ぶと、設定画面の保存 1 回ごとに再登録のマルチキャストが飛ぶ。逆に 呼ばないと、改名してもドロップダウンに最大 75
    分（PTR の other-TTL）古い名前が 残る。どちらの誤りも黙って通るのでここで条件をピンする。
    """

    @pytest.fixture
    def advertiser(self, app: FastAPI, client: TestClient) -> RecordingAdvertiser:
        """広告を記録する受け口を app に差す（lifespan 起動後なので start されない）."""
        recording = RecordingAdvertiser()
        app.state.advertiser = recording
        return recording

    @staticmethod
    def _put(client: TestClient, values: dict[str, object]) -> None:
        response = client.put("/api/settings/machine", json={"values": values})
        assert response.status_code == 200, response.text

    def test_changing_the_name_publishes_it(
        self, client: TestClient, advertiser: RecordingAdvertiser
    ):
        self._put(client, {"machine_name": "改名後の機体"})

        assert advertiser.updates == ["改名後の機体"]

    def test_saving_other_fields_does_not_publish(
        self, client: TestClient, advertiser: RecordingAdvertiser
    ):
        self._put(client, {"paste_dispenser.max_fill_speed": 0.9})

        assert advertiser.updates == []

    def test_saving_the_same_name_again_does_not_publish(
        self, client: TestClient, advertiser: RecordingAdvertiser
    ):
        """設定画面は全項目を送るので、同じ名前の再保存が最も多い経路."""
        self._put(client, {"machine_name": "同じ名前"})
        assert advertiser.updates == ["同じ名前"]

        self._put(client, {"machine_name": "同じ名前"})

        assert advertiser.updates == ["同じ名前"]
