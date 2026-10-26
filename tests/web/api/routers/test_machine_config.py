"""`web.api.routers.machine_config` の仕様テスト.

- GET /api/machine_config が全設定項目を、表示の定義（見出し・カード・表示名・単位・選択肢）と値付きで表示する順に返す
- PUT は実ファイルへ反映しコメントを保持する
- 未知キー → 400、busy → 409

- PUT /api/machine_config で camera.* キーを書いたら FrameHub を再構築する
- camera.crop.* は再構築条件から除外する（レンダラがフレーム毎に読むため
  デバイス再構築不要。crop 変更でストリームを切断しない）
- camera.crop.* は 1 以上の int（0 / 負値 → 400）
"""

from pathlib import Path

import pytest
import tomlkit
from fastapi.testclient import TestClient

from pcbasm.config import MachineConfig, load_machine
from pcbasm.config.fields import setting_fields
from web.api.backend import Backend


class TestPasteSettingsValidation:
    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("solder.nozzle.diameter", 0.0),
            ("solder.motion.max_fill_speed", 0.0),
            ("solder.dispenser.max_dispense_rate", 0.0),
            ("solder.dispenser.retract_amount", 0.0),
            ("solder.dispenser.retract_accel_factor", 1.0),
            ("solder.defaults.bead_width_factor", 0.0),
            ("solder.defaults.overlap", -0.1),
            ("solder.defaults.overlap", 1.0),
            ("solder.defaults.ul_per_mm2", 0.0),
            ("solder.defaults.prime_extra_delay", -0.1),
        ],
    )
    def test_invalid_batch_preserves_the_usable_config(
        self, client: TestClient, config_dir: Path, key: str, value: float
    ):
        path = config_dir / "machine.toml"
        original = path.read_bytes()

        response = client.put(
            "/api/machine_config",
            json={
                "values": {
                    "camera.calibration_file": "must-not-be-saved.json",
                    key: value,
                }
            },
        )

        assert response.status_code == 400, response.text
        assert key.rsplit(".", 1)[-1] in response.json()["detail"]
        assert path.read_bytes() == original
        assert load_machine(path).require_solder().nozzle.diameter > 0

    def test_an_existing_invalid_setting_can_be_repaired_one_field_at_a_time(
        self, client: TestClient, config_dir: Path
    ):
        path = config_dir / "machine.toml"
        document = tomlkit.parse(path.read_text())
        document["solder"]["nozzle"]["diameter"] = 0.0
        document["solder"]["motion"]["max_fill_speed"] = 0.0
        path.write_text(tomlkit.dumps(document))

        # 他の不正値が残っていても画面を開けて、編集した値だけを直せる。
        assert client.get("/api/machine_config").status_code == 200
        for key, value in (
            ("solder.nozzle.diameter", 0.3),
            ("solder.motion.max_fill_speed", 2.0),
        ):
            response = client.put("/api/machine_config", json={"values": {key: value}})
            assert response.status_code == 200, response.text
        solder = load_machine(path).require_solder()
        assert solder.nozzle.diameter == 0.3
        assert solder.motion.max_fill_speed == 2.0


class TestMachineSettingsApi:
    """GET / PUT /api/machine_config."""

    def test_get_returns_every_setting_field_in_display_order(self, client: TestClient):
        """項目は setting_fields() の全項目で、その並び（設定ページに描く順）のまま返る."""
        response = client.get("/api/machine_config")

        assert response.status_code == 200
        keys = [field["key"] for field in response.json()["fields"]]
        assert keys == [field.key for field in setting_fields()]
        fields = {field["key"]: field for field in response.json()["fields"]}
        assert all(field["label"] and field["group"] for field in fields.values())

        # 代表 2 型: そのまま返る float と、"auto" を取り得る float_or_auto
        assert fields["solder.motion.max_fill_speed"]["value"] == 0.8
        assert fields["solder.motion.max_fill_speed"]["value_type"] == "float"
        assert fields["solder.defaults.paste_height"]["value"] == "auto"
        assert fields["solder.defaults.paste_height"]["value_type"] == "float_or_auto"

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            pytest.param("solder.defaults.dispense_mode", "line", id="dispense_mode"),
            pytest.param(
                "solder.defaults.dispense_mode",
                "micro_line",
                id="micro_line_mode",
            ),
            pytest.param("solder.nozzle.outer_diameter", 0.4, id="nozzle_outer"),
            pytest.param(
                "solder.motion.micro_line_settle_seconds", 2.0, id="micro_line"
            ),
            pytest.param(
                "solder.defaults.micro_line_z_start",
                "auto",
                id="micro_line_auto",
            ),
            pytest.param(
                "solder.motion.post_dispense_settle_seconds",
                0.3,
                id="post_settle",
            ),
            pytest.param(
                "solder.defaults.line_direction",
                "outward",
                id="line_direction",
            ),
            pytest.param("solder.defaults.paste_height", "auto", id="float_or_auto"),
            pytest.param("solder.auto.line_aspect_ratio", 1.7, id="float"),
            pytest.param("audio.device", "plughw:CARD=Audio,DEV=0", id="str"),
            pytest.param("audio.volume", 0.4, id="audio-volume"),
            pytest.param(
                "reference_point.offsets.bottom_right", [-4.0, 4.0], id="float_pair"
            ),
            pytest.param("alignment.region_size_px", 160, id="int"),
            pytest.param("alignment.region_overlap", 0.25, id="region-overlap"),
            # 負の外周余白は pad 外形をはみ出して塗る指定
            pytest.param(
                "solder.defaults.boundary_margin",
                -0.2,
                id="negative-boundary-margin",
            ),
        ],
    )
    def test_put_writes_the_value_and_echoes_it_back(
        self, client: TestClient, key: str, value: object
    ):
        """value_type ごとに、PUT した値がそのまま応答の `value` に戻る."""
        response = client.put("/api/machine_config", json={"values": {key: value}})

        assert response.status_code == 200, response.text
        field = next(item for item in response.json()["fields"] if item["key"] == key)
        assert field["value"] == value

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            pytest.param("solder.no_such_key", 1.0, id="unknown-key"),
            pytest.param(
                "reference_point.offsets.top_left", [1.0], id="short-float-pair"
            ),
            # bool を受け付けるフィールドは無く、数値へ暗黙変換もされない
            pytest.param("solder.motion.max_fill_speed", True, id="bool-for-float"),
            pytest.param(
                "solder.defaults.dispense_mode",
                "spray",
                id="unknown-dispense-mode",
            ),
            pytest.param(
                "solder.defaults.line_direction",
                "sideways",
                id="unknown-line-direction",
            ),
            pytest.param(
                "solder.auto.line_aspect_ratio", 1.0, id="auto-line-ratio-at-1"
            ),
            pytest.param("probe.board_edge_margin", 0.0, id="non-positive-margin"),
            pytest.param("solder.motion.lift_height", 0.0, id="non-positive-lift"),
            pytest.param("audio.device", " ", id="blank-audio-device"),
            pytest.param("audio.volume", -0.1, id="audio-volume-below-range"),
            pytest.param("audio.volume", 1.1, id="audio-volume-above-range"),
            pytest.param(
                "alignment.refine_max_short_side",
                True,
                id="bool-refinement-threshold",
            ),
            pytest.param(
                "alignment.refine_max_short_side",
                -0.01,
                id="negative-refinement-threshold",
            ),
            pytest.param(
                "alignment.region_overlap",
                -0.01,
                id="overlap-below-range",
            ),
            pytest.param("alignment.region_overlap", 1.0, id="overlap-at-1"),
            # camera.crop.* は 1 以上の int
            pytest.param("camera.crop.width", 0, id="zero-crop-width"),
            pytest.param("camera.crop.height", -1, id="negative-crop-height"),
        ],
    )
    def test_put_invalid_value_returns_400(
        self, client: TestClient, key: str, value: object
    ):
        """値そのものの検証は machine_config が担う。ここは 400 写像と、原因キーを名指しすることを見る."""
        response = client.put("/api/machine_config", json={"values": {key: value}})

        assert response.status_code == 400
        assert key.rsplit(".", 1)[-1] in response.text

    @pytest.mark.parametrize(
        "steps",
        [
            pytest.param(
                [{"solder.nozzle.outer_diameter": 0.2}], id="outer-below-inner"
            ),
            pytest.param(
                [
                    {"solder.nozzle.outer_diameter": 0.4},
                    {"solder.nozzle.diameter": 0.5},
                ],
                id="inner-above-saved-outer",
            ),
        ],
    )
    def test_put_rejects_outer_diameter_below_inner_diameter(
        self, client: TestClient, config_dir: Path, steps: list[dict]
    ):
        """外径と内径は片方ずつ編集できるので、TOML に残る値と合わせて相互制約を見る.

        ここで拒否しないと、保存はできるのに ``MachineConfig.require_solder()`` が読めない machine.toml になる。
        """
        *accepted, rejected = steps
        for values in accepted:
            response = client.put("/api/machine_config", json={"values": values})
            assert response.status_code == 200

        response = client.put("/api/machine_config", json={"values": rejected})

        assert response.status_code == 400
        assert "outer_diameter" in response.text
        assert load_machine(config_dir / "machine.toml").solder is not None

    def test_get_reports_none_for_missing_keys(self, client: TestClient):
        fields = {
            field["key"]: field
            for field in client.get("/api/machine_config").json()["fields"]
        }

        assert fields["solder.defaults.bead_width_factor"]["value"] is None
        assert fields["probe.lift_height"]["value"] is None

    def test_get_resolves_defaults_for_missing_keys(self, client: TestClient):
        """未記載キーは `value` が None でも `resolved` に実効値（既定値）が入る.

        SSR ページはこの実効値で現在値を描く。frontend 側で 0 などに代替すると、その値が 「設定に保存」で
        machine.toml へ書き戻されて装置の挙動を壊す。
        """
        fields = {
            field["key"]: field
            for field in client.get("/api/machine_config").json()["fields"]
        }

        assert fields["solder.defaults.bead_width_factor"]["resolved"] == 1.0
        assert fields["probe.lift_height"]["resolved"] == 1.0
        # 記載があるキーは書かれている値がそのまま実効値
        assert fields["solder.motion.max_fill_speed"]["resolved"] == 0.8
        assert fields["solder.defaults.paste_height"]["resolved"] == "auto"
        assert fields["reference_point.offsets.top_left"]["resolved"] == [5.0, -5.0]

    def test_get_returns_written_values_without_resolved_while_degraded(
        self, client: TestClient, config_dir: Path
    ):
        """設定エラーの間も記載値は返し、設定ページから直せるようにする.

        `[probe]` の `min_radius` は既定値を持たないので、消すと全体の検証に通らない。
        部分的に解決した実効値は混ぜない（理由は `/api/session/state` の config_error に出る）。
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
            for field in client.get("/api/machine_config").json()["fields"]
        }

        assert fields["probe.min_samples"]["resolved"] is None
        assert fields["probe.min_samples"]["value"] == 6
        assert fields["alignment.canny_low"]["resolved"] is None
        assert fields["alignment.canny_low"]["value"] == 81.0

    def test_put_writes_file_and_preserves_comments(
        self, client: TestClient, config_dir: Path
    ):
        path = config_dir / "machine.toml"
        before = path.read_text(encoding="utf-8").splitlines()

        response = client.put(
            "/api/machine_config",
            json={
                "values": {
                    "solder.dispenser.rotations_per_ul": 9.876543,
                    "solder.motion.max_fill_speed": 0.9,
                    "probe.min_radius": 2.5,
                    "probe.board_edge_margin": 3.0,
                    "probe.min_samples": 7,
                }
            },
        )

        assert response.status_code == 200
        fields = {field["key"]: field for field in response.json()["fields"]}
        assert fields["solder.dispenser.rotations_per_ul"]["value"] == 9.876543
        assert fields["solder.motion.max_fill_speed"]["value"] == 0.9
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

    def test_get_returns_the_display_definition_from_the_schema(
        self, client: TestClient
    ):
        """見出し・カード・選択肢・下限・空欄の扱いは backend が設定のスキーマから組んで返す."""
        fields = {
            field["key"]: field
            for field in client.get("/api/machine_config").json()["fields"]
        }

        mode = fields["solder.defaults.dispense_mode"]
        assert mode["section"] == "solder"
        assert mode["section_label"] == "はんだ塗布"
        assert mode["group"] == "塗布方式"
        assert mode["value_type"] == "choice"
        assert {"value": "micro_line", "label": "微小線"} in mode["choices"]
        assert fields["solder.nozzle.clean.passes"]["minimum"] == 0
        assert fields["solder.nozzle.clean.wipe_speed"]["minimum"] is None
        calibration = fields["solder.volume_correction.calibration_file"]
        assert calibration["clearable"] is True
        assert fields["solder.nozzle.cap.position"]["value_type"] == ("float_triple")

    def test_get_returns_reference_point_offset_pairs(self, client: TestClient):
        fields = {
            field["key"]: field
            for field in client.get("/api/machine_config").json()["fields"]
        }

        top_left = fields["reference_point.offsets.top_left"]
        assert top_left["value"] == [5.0, -5.0]
        assert top_left["value_type"] == "float_pair"
        assert fields["reference_point.offsets.bottom_right"]["value"] == [-5.0, 5.0]

    def test_put_while_busy_returns_409(self, client: TestClient, backend: Backend):
        with backend.lock.hold("pytest-job"):
            response = client.put(
                "/api/machine_config",
                json={"values": {"solder.motion.max_fill_speed": 0.9}},
            )

        assert response.status_code == 409


class TestPadAlignRegionSettingsApi:
    """重複領域の寸法と overlap を GET / PUT できる."""

    def test_get_reports_unset_refinement_threshold_with_resolved_default(
        self, client: TestClient
    ):
        fields = {
            field["key"]: field
            for field in client.get("/api/machine_config").json()["fields"]
        }

        threshold = fields["alignment.refine_max_short_side"]
        assert threshold["label"] == "逐次位置合わせ対象の最大短辺"
        assert threshold["value_type"] == "float"
        assert threshold["unit"] == "mm"
        assert threshold["value"] is None
        assert threshold["resolved"] == pytest.approx(0.4)

    def test_put_zero_disables_refinement_and_persists_value(
        self, client: TestClient, config_dir: Path
    ):
        key = "alignment.refine_max_short_side"

        response = client.put("/api/machine_config", json={"values": {key: 0.0}})

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
    """camera.* キーの保存による FrameHub 再構築.

    camera.crop.* はレンダラがフレーム毎に読むため再構築対象から除外される （camera.fps 等の他 camera.*
    キーは従来どおり再構築する）。
    """

    def test_put_camera_key_rebuilds_frame_hub(
        self, fake_camera_client: TestClient, fake_camera_backend: Backend
    ):
        hub = fake_camera_backend.devices.frame_hub()

        response = fake_camera_client.put(
            "/api/machine_config",
            json={"values": {"camera.fps": 20.0}},
        )

        assert response.status_code == 200
        assert fake_camera_backend.devices.frame_hub() is not hub

    def test_put_without_camera_key_keeps_frame_hub(
        self, fake_camera_client: TestClient, fake_camera_backend: Backend
    ):
        hub = fake_camera_backend.devices.frame_hub()

        response = fake_camera_client.put(
            "/api/machine_config",
            json={"values": {"solder.motion.max_fill_speed": 0.9}},
        )

        assert response.status_code == 200
        assert fake_camera_backend.devices.frame_hub() is hub

    def test_put_camera_crop_key_keeps_frame_hub(
        self, fake_camera_client: TestClient, fake_camera_backend: Backend
    ):
        """Crop は camera.* 前置だが再構築しない（ストリーム非切断の要件）."""
        hub = fake_camera_backend.devices.frame_hub()

        response = fake_camera_client.put(
            "/api/machine_config",
            json={"values": {"camera.crop.width": 300, "camera.crop.height": 300}},
        )

        assert response.status_code == 200, response.text
        assert fake_camera_backend.devices.frame_hub() is hub

    def test_put_camera_crop_and_other_camera_key_rebuilds_frame_hub(
        self, fake_camera_client: TestClient, fake_camera_backend: Backend
    ):
        """Camera.crop.* と他の camera.* キーが混在した PUT では再構築する（camera.crop.* 以外の
        camera.* が 1 つでも含まれていれば rebuild する境界のピン）."""
        hub = fake_camera_backend.devices.frame_hub()

        response = fake_camera_client.put(
            "/api/machine_config",
            json={"values": {"camera.fps": 20.0, "camera.crop.width": 300}},
        )

        assert response.status_code == 200, response.text
        assert fake_camera_backend.devices.frame_hub() is not hub
