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

import pytest
import tomlkit
from fastapi.testclient import TestClient

from pcbasm.config import Machine
from web.api.config_store import MACHINE_FIELDS
from web.api.state import AppState


class TestPasteSettingsValidation:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("nozzle_diameter", 0.0),
            ("max_fill_speed", 0.0),
            ("max_dispense_rate", 0.0),
            ("retract_amount", 0.0),
            ("retract_accel_factor", 1.0),
            ("bead_width_factor", 0.0),
            ("overlap", -0.1),
            ("overlap", 1.0),
            ("boundary_margin", -0.1),
            ("ul_per_mm2", 0.0),
            ("prime_extra_delay", -0.1),
        ],
    )
    def test_invalid_batch_preserves_the_usable_config(
        self, client: TestClient, config_dir: Path, field: str, value: float
    ):
        path = config_dir / "machine.toml"
        original = path.read_bytes()

        response = client.put(
            "/api/settings/machine",
            json={
                "values": {
                    "camera.calibration_file": "must-not-be-saved.json",
                    f"paste_dispenser.{field}": value,
                }
            },
        )

        assert response.status_code == 400, response.text
        assert field in response.json()["detail"]
        assert path.read_bytes() == original
        assert Machine(path).paste_dispenser.nozzle_diameter > 0

    def test_an_existing_invalid_setting_can_be_repaired_one_field_at_a_time(
        self, client: TestClient, config_dir: Path
    ):
        path = config_dir / "machine.toml"
        document = tomlkit.parse(path.read_text())
        document["paste_dispenser"]["nozzle_diameter"] = 0.0
        document["paste_dispenser"]["max_fill_speed"] = 0.0
        path.write_text(tomlkit.dumps(document))

        # 他の不正値が残っていても画面を開けて、編集した値だけを直せる。
        assert client.get("/api/settings/machine").status_code == 200
        for field, value in (("nozzle_diameter", 0.3), ("max_fill_speed", 2.0)):
            response = client.put(
                "/api/settings/machine",
                json={"values": {f"paste_dispenser.{field}": value}},
            )
            assert response.status_code == 200, response.text
        dispenser = Machine(path).paste_dispenser
        assert dispenser.nozzle_diameter == 0.3
        assert dispenser.max_fill_speed == 2.0


class TestMachineSettingsApi:
    """GET / PUT /api/settings/machine."""

    def test_get_returns_every_whitelisted_field(self, client: TestClient):
        """公開キー集合は MACHINE_FIELDS そのもので、各項目が label / value / value_type を持つ."""
        response = client.get("/api/settings/machine")

        assert response.status_code == 200
        fields = {field["key"]: field for field in response.json()["fields"]}
        assert set(fields) == {spec.key for spec in MACHINE_FIELDS}
        assert all(field["label"] for field in fields.values())

        # 代表 2 型: そのまま返る float と、"auto" を取り得る float_or_auto
        assert fields["paste_dispenser.max_fill_speed"]["value"] == 0.8
        assert fields["paste_dispenser.max_fill_speed"]["value_type"] == "float"
        assert fields["paste_dispenser.paste_height"]["value"] == "auto"
        assert fields["paste_dispenser.paste_height"]["value_type"] == "float_or_auto"

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            pytest.param("paste_dispenser.dispense_mode", "line", id="dispense_mode"),
            pytest.param(
                "paste_dispenser.line_direction", "outward", id="line_direction"
            ),
            pytest.param("paste_dispenser.paste_height", "auto", id="float_or_auto"),
            pytest.param("paste_dispenser.auto_line_aspect_ratio", 1.7, id="float"),
            pytest.param("audio.device", "plughw:CARD=Audio,DEV=0", id="str"),
            pytest.param("audio.volume", 0.4, id="audio-volume"),
            pytest.param(
                "reference_point.offsets.bottom_right", [-4.0, 4.0], id="float_pair"
            ),
            pytest.param("paste_dispenser.pad_align.region_size_px", 160, id="int"),
            pytest.param(
                "paste_dispenser.pad_align.region_overlap", 0.25, id="region-overlap"
            ),
        ],
    )
    def test_put_writes_the_value_and_echoes_it_back(
        self, client: TestClient, key: str, value: object
    ):
        """value_type ごとに、PUT した値がそのまま応答の `value` に戻る."""
        response = client.put("/api/settings/machine", json={"values": {key: value}})

        assert response.status_code == 200, response.text
        field = next(item for item in response.json()["fields"] if item["key"] == key)
        assert field["value"] == value

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            pytest.param("paste_dispenser.no_such_key", 1.0, id="unknown-key"),
            pytest.param(
                "reference_point.offsets.top_left", [1.0], id="short-float-pair"
            ),
            # bool を受け付けるフィールドは無く、数値へ暗黙変換もされない
            pytest.param("paste_dispenser.max_fill_speed", True, id="bool-for-float"),
            pytest.param(
                "paste_dispenser.dispense_mode", "spray", id="unknown-dispense-mode"
            ),
            pytest.param(
                "paste_dispenser.line_direction",
                "sideways",
                id="unknown-line-direction",
            ),
            pytest.param(
                "paste_dispenser.auto_line_aspect_ratio", 1.0, id="auto-line-ratio-at-1"
            ),
            pytest.param("probe.board_edge_margin", 0.0, id="non-positive-margin"),
            pytest.param("paste_dispenser.lift_height", 0.0, id="non-positive-lift"),
            pytest.param("audio.device", " ", id="blank-audio-device"),
            pytest.param("audio.volume", -0.1, id="audio-volume-below-range"),
            pytest.param("audio.volume", 1.1, id="audio-volume-above-range"),
            pytest.param(
                "paste_dispenser.pad_align.refine_max_short_side",
                True,
                id="bool-refinement-threshold",
            ),
            pytest.param(
                "paste_dispenser.pad_align.refine_max_short_side",
                -0.01,
                id="negative-refinement-threshold",
            ),
            pytest.param(
                "paste_dispenser.pad_align.region_overlap",
                -0.01,
                id="overlap-below-range",
            ),
            pytest.param(
                "paste_dispenser.pad_align.region_overlap", 1.0, id="overlap-at-1"
            ),
            # camera.crop.* は 1 以上の int（webui-camera-calib 計画書・要確認事項 2）
            pytest.param("camera.crop.width", 0, id="zero-crop-width"),
            pytest.param("camera.crop.height", -1, id="negative-crop-height"),
        ],
    )
    def test_put_invalid_value_returns_400(
        self, client: TestClient, key: str, value: object
    ):
        """値そのものの検証は config_store が担う。ここは 400 写像と、原因キーを名指しすることを見る."""
        response = client.put("/api/settings/machine", json={"values": {key: value}})

        assert response.status_code == 400
        assert key.rsplit(".", 1)[-1] in response.text

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

    def test_get_returns_reference_point_offset_pairs(self, client: TestClient):
        fields = {
            field["key"]: field
            for field in client.get("/api/settings/machine").json()["fields"]
        }

        top_left = fields["reference_point.offsets.top_left"]
        assert top_left["value"] == [5.0, -5.0]
        assert top_left["value_type"] == "float_pair"
        assert fields["reference_point.offsets.bottom_right"]["value"] == [-5.0, 5.0]

    def test_put_while_busy_returns_409(self, client: TestClient, appstate: AppState):
        with appstate.machine_lock("pytest-job"):
            response = client.put(
                "/api/settings/machine",
                json={"values": {"paste_dispenser.max_fill_speed": 0.9}},
            )

        assert response.status_code == 409


class TestPadAlignRegionSettingsApi:
    """重複領域の寸法と overlap を GET / PUT できる."""

    def test_get_reports_unset_refinement_threshold_with_resolved_default(
        self, client: TestClient
    ):
        fields = {
            field["key"]: field
            for field in client.get("/api/settings/machine").json()["fields"]
        }

        threshold = fields["paste_dispenser.pad_align.refine_max_short_side"]
        assert threshold["label"] == "逐次位置合わせ対象の最大短辺"
        assert threshold["value_type"] == "float"
        assert threshold["unit"] == "mm"
        assert threshold["value"] is None
        assert threshold["resolved"] == pytest.approx(0.4)

    def test_put_zero_disables_refinement_and_persists_value(
        self, client: TestClient, config_dir: Path
    ):
        key = "paste_dispenser.pad_align.refine_max_short_side"

        response = client.put("/api/settings/machine", json={"values": {key: 0.0}})

        assert response.status_code == 200, response.text
        field = next(
            field for field in response.json()["fields"] if field["key"] == key
        )
        assert field["value"] == pytest.approx(0.0)
        assert field["resolved"] == pytest.approx(0.0)
        assert "refine_max_short_side = 0.0" in (config_dir / "machine.toml").read_text(
            encoding="utf-8"
        )


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
