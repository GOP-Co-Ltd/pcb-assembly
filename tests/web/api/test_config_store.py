"""`web.api.config_store.ConfigStore` の仕様テスト.

計画書「`src/webui/config_store.py`」節が契約:

- machine.toml のホワイトリスト読み書き
- tomlkit によるコメント・構造保持（変更対象外の行は不変）
- 未知キー / 型不一致 → UnknownFieldError

計画書 memory/agents/implementation-planner/webui-camera-calib.md
「公開インターフェース案 4」+ 要確認事項 2（orchestrator 採用）が追記契約:

- camera.crop.width / camera.crop.height は 1 以上の int（0 / 負値は
  UnknownFieldError）。change 即自動保存 UI での事故防止

MR2（計画書 docs/plans/web-api-ui-split.md「MR2」節）が追記契約:

- machine_name（ドットの無いトップレベル bare key）を書き込める。tomlkit が
  トップレベルへ挿入する挙動に依存するので、書込後に tomllib で再パースして
  トップレベルに残ることをピンする（テーブルへ吸い込まれたら気づけるように）
"""

import tomllib
from pathlib import Path
from typing import cast

import pytest

from web.api.config_store import (
    MACHINE_FIELDS,
    ConfigStore,
    MachineSettingValue,
    UnknownFieldError,
)


class TestMachineSettings:
    """machine.toml のホワイトリスト読み書き."""

    def test_read_returns_values_with_declared_types(self, store: ConfigStore):
        values = store.read_machine_settings()

        assert values["paste_dispenser.dispense_mode"] == "auto"
        assert values["paste_dispenser.line_direction"] is None
        assert values["paste_dispenser.auto_line_aspect_ratio"] == 1.618
        assert values["paste_dispenser.max_fill_speed"] == 0.8
        assert values["paste_dispenser.solder_paste_density"] == 3.78
        assert values["paste_dispenser.paste_height"] == "auto"
        assert values["paste_dispenser.lift_height"] == 2.0
        assert values["paste_dispenser.toolhead.x"] == -1.772
        assert values["probe.min_radius"] == 0.7
        assert values["probe.board_edge_margin"] == 2.5
        assert values["camera.device_id"] == 0
        assert values["camera.format"] == "YUYV"
        assert values["camera.crop.width"] == 600
        assert values["reference_point.target_diameter"] == 3.0

    def test_read_covers_every_whitelisted_key(self, store: ConfigStore):
        values = store.read_machine_settings()

        assert set(values) == {spec.key for spec in MACHINE_FIELDS}

    def test_initial_purge_ul_is_whitelisted(self):
        assert "paste_dispenser.initial_purge_ul" in {
            spec.key for spec in MACHINE_FIELDS
        }

    def test_missing_keys_read_as_none(self, store: ConfigStore):
        values = store.read_machine_settings()

        assert values["paste_dispenser.initial_purge_ul"] is None
        assert values["paste_dispenser.bead_width_factor"] is None
        assert values["paste_dispenser.boundary_margin"] is None
        assert values["paste_dispenser.auto_area_short_side_factor"] is None
        assert values["paste_dispenser.pad_align.refine_max_short_side"] is None
        assert values["probe.lift_height"] is None

    def test_write_then_reread_reflects_value(self, store: ConfigStore):
        store.write_machine_settings({"paste_dispenser.max_fill_speed": 0.9})

        values = store.read_machine_settings()
        assert values["paste_dispenser.max_fill_speed"] == 0.9

    def test_write_dispense_mode_then_reread_reflects_value(self, store: ConfigStore):
        store.write_machine_settings({"paste_dispenser.dispense_mode": "line"})

        values = store.read_machine_settings()
        assert values["paste_dispenser.dispense_mode"] == "line"

    def test_write_line_direction_then_reread_reflects_value(self, store: ConfigStore):
        store.write_machine_settings({"paste_dispenser.line_direction": "outward"})

        values = store.read_machine_settings()
        assert values["paste_dispenser.line_direction"] == "outward"

    def test_write_solder_paste_density_then_reread_reflects_value(
        self, store: ConfigStore
    ):
        store.write_machine_settings({"paste_dispenser.solder_paste_density": 4.1})

        values = store.read_machine_settings()
        assert values["paste_dispenser.solder_paste_density"] == 4.1

    def test_write_initial_purge_ul_then_reread_reflects_value(
        self, store: ConfigStore
    ):
        store.write_machine_settings({"paste_dispenser.initial_purge_ul": 0.2})

        values = store.read_machine_settings()
        assert values["paste_dispenser.initial_purge_ul"] == 0.2

    @pytest.mark.parametrize("height", [0.0, -1.0])
    def test_write_rejects_non_positive_lift_height(
        self, store: ConfigStore, height: float
    ):
        with pytest.raises(UnknownFieldError, match="lift_height"):
            store.write_machine_settings({"paste_dispenser.lift_height": height})

    def test_write_zero_initial_purge_ul_disables_purge(self, store: ConfigStore):
        store.write_machine_settings({"paste_dispenser.initial_purge_ul": 0.0})

        values = store.read_machine_settings()
        assert values["paste_dispenser.initial_purge_ul"] == 0.0

    def test_write_auto_paste_height_then_reread_reflects_value(
        self, store: ConfigStore
    ):
        store.write_machine_settings({"paste_dispenser.paste_height": 0.25})
        store.write_machine_settings({"paste_dispenser.paste_height": "auto"})

        values = store.read_machine_settings()
        assert values["paste_dispenser.paste_height"] == "auto"

    def test_write_lift_height_then_reread_reflects_value(self, store: ConfigStore):
        store.write_machine_settings({"paste_dispenser.lift_height": 3.25})

        values = store.read_machine_settings()
        assert values["paste_dispenser.lift_height"] == 3.25

    def test_write_keeps_untouched_lines_byte_identical(
        self, store: ConfigStore, config_dir: Path
    ):
        path = config_dir / "machine.toml"
        before = path.read_text(encoding="utf-8").splitlines()

        store.write_machine_settings({"paste_dispenser.max_fill_speed": 0.9})

        after = path.read_text(encoding="utf-8").splitlines()
        assert len(after) == len(before)
        changed = [(b, a) for b, a in zip(before, after) if b != a]
        assert len(changed) == 1
        assert "max_fill_speed" in changed[0][0]

    def test_write_keeps_inline_comment_on_changed_line(
        self, store: ConfigStore, config_dir: Path
    ):
        store.write_machine_settings({"probe.min_radius": 2.5})

        path = config_dir / "machine.toml"
        line = next(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("min_radius")
        )
        assert "2.5" in line
        assert "銅箔境界" in line

    def test_write_adds_whitelisted_key_missing_from_toml(self, store: ConfigStore):
        store.write_machine_settings({"paste_dispenser.bead_width_factor": 1.5})

        values = store.read_machine_settings()
        assert values["paste_dispenser.bead_width_factor"] == 1.5

    def test_write_adds_probe_lift_height_missing_from_toml(
        self, store: ConfigStore, config_dir: Path
    ):
        store.write_machine_settings({"probe.lift_height": 1.25})

        values = store.read_machine_settings()
        assert values["probe.lift_height"] == 1.25
        text = (config_dir / "machine.toml").read_text(encoding="utf-8")
        assert "lift_height = 1.25" in text

    def test_write_board_edge_margin_then_reread_reflects_value(
        self, store: ConfigStore
    ):
        store.write_machine_settings({"probe.board_edge_margin": 3.0})

        values = store.read_machine_settings()
        assert values["probe.board_edge_margin"] == 3.0

    def test_read_includes_camera_calibration_file(self, store: ConfigStore):
        """Phase 4: camera.calibration_file がホワイトリストに含まれ既存値が読める."""
        values = store.read_machine_settings()

        assert values["camera.calibration_file"] == "ov9281_test_fixture.json"

    def test_write_calibration_file_changes_only_target_line(
        self, store: ConfigStore, config_dir: Path
    ):
        """camera.calibration_file の書込は対象行のみ変更しコメント・構造を保つ."""
        path = config_dir / "machine.toml"
        before = path.read_text(encoding="utf-8").splitlines()

        store.write_machine_settings(
            {"camera.calibration_file": "ov9281_20260612.json"}
        )

        values = store.read_machine_settings()
        assert values["camera.calibration_file"] == "ov9281_20260612.json"
        after = path.read_text(encoding="utf-8").splitlines()
        assert len(after) == len(before)
        changed = [(b, a) for b, a in zip(before, after) if b != a]
        assert len(changed) == 1
        assert "calibration_file" in changed[0][0]

    def test_write_machine_name_stays_a_top_level_bare_key(
        self, store: ConfigStore, config_dir: Path
    ):
        """machine_name はテーブルに吸い込まれずトップレベルに残る（tomllib で再パース）.

        tomlkit がドットの無いキーを `[klipper]` などのテーブル内へ挿入すると
        `Machine.machine_name` から読めなくなるため、挙動を明示的にピンする。
        """
        path = config_dir / "machine.toml"

        store.write_machine_settings({"machine_name": "黒兎 2 号機"})

        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        assert parsed["machine_name"] == "黒兎 2 号機"
        assert store.read_machine_settings()["machine_name"] == "黒兎 2 号機"

    def test_write_machine_name_preserves_comments_and_other_lines(
        self, store: ConfigStore, config_dir: Path
    ):
        """行の追加は machine_name の 1 行だけ。既存のコメント・構造は不変."""
        path = config_dir / "machine.toml"
        before = path.read_text(encoding="utf-8").splitlines()

        store.write_machine_settings({"machine_name": "黒兎 2 号機"})

        after = path.read_text(encoding="utf-8").splitlines()
        assert len(after) == len(before) + 1
        added = [line for line in after if line not in before]
        assert added == ['machine_name = "黒兎 2 号機"']

    def test_non_string_machine_name_raises(self, store: ConfigStore):
        with pytest.raises(UnknownFieldError, match="machine_name"):
            store.write_machine_settings({"machine_name": 2.0})

    def test_non_string_calibration_file_raises(self, store: ConfigStore):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings({"camera.calibration_file": 1.0})

    def test_unknown_key_raises_unknown_field_error(self, store: ConfigStore):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings({"paste_dispenser.no_such_key": 1.0})

    def test_type_mismatch_raises_unknown_field_error(self, store: ConfigStore):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings({"paste_dispenser.max_fill_speed": "fast"})

    def test_numeric_field_rejects_bool(self, store: ConfigStore):
        # bool は int のサブクラスなので、数値フィールドへの bool 投入は拒否する
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings({"paste_dispenser.max_fill_speed": True})

    def test_unknown_dispense_mode_raises_unknown_field_error(self, store: ConfigStore):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings({"paste_dispenser.dispense_mode": "spray"})

    def test_unknown_line_direction_raises_unknown_field_error(
        self, store: ConfigStore
    ):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings({"paste_dispenser.line_direction": "sideways"})

    def test_auto_line_aspect_ratio_must_exceed_one(self, store: ConfigStore):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings(
                {"paste_dispenser.auto_line_aspect_ratio": 1.0}
            )

    @pytest.mark.parametrize("factor", [0.0, -1.0])
    def test_auto_area_short_side_factor_must_be_positive(
        self, store: ConfigStore, factor: float
    ):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings(
                {"paste_dispenser.auto_area_short_side_factor": factor}
            )

    def test_write_auto_area_short_side_factor_then_reread_reflects_value(
        self, store: ConfigStore
    ):
        store.write_machine_settings(
            {"paste_dispenser.auto_area_short_side_factor": 4.0}
        )

        values = store.read_machine_settings()
        assert values["paste_dispenser.auto_area_short_side_factor"] == 4.0

    def test_manual_paste_height_must_be_positive(self, store: ConfigStore):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings({"paste_dispenser.paste_height": 0.0})

    def test_solder_paste_density_must_be_positive(self, store: ConfigStore):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings({"paste_dispenser.solder_paste_density": 0.0})

    @pytest.mark.parametrize("board_edge_margin", [0.0, -1.0])
    def test_board_edge_margin_must_be_positive(
        self, store: ConfigStore, board_edge_margin: float
    ):
        with pytest.raises(UnknownFieldError, match="board_edge_margin"):
            store.write_machine_settings({"probe.board_edge_margin": board_edge_margin})

    def test_initial_purge_ul_must_not_be_negative(self, store: ConfigStore):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings({"paste_dispenser.initial_purge_ul": -0.01})

    def test_non_integral_float_for_int_field_raises(self, store: ConfigStore):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings({"paste_dispenser.pad_align.blur_ksize": 5.5})

    def test_missing_machine_toml_raises_file_not_found(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            ConfigStore(tmp_path / "no-such-config").read_machine_settings()


class TestReferencePointOffsets:
    """float_pair 型フィールド reference_point.offsets.* の読み書き."""

    def test_read_returns_all_four_corner_pairs(self, store: ConfigStore):
        values = store.read_machine_settings()

        assert values["reference_point.offsets.top_left"] == [5.0, -5.0]
        assert values["reference_point.offsets.top_right"] == [-5.0, -5.0]
        assert values["reference_point.offsets.bottom_left"] == [5.0, 5.0]
        assert values["reference_point.offsets.bottom_right"] == [-5.0, 5.0]

    def test_write_corner_then_reread_reflects_pair(self, store: ConfigStore):
        store.write_machine_settings(
            {"reference_point.offsets.bottom_right": [-4.0, 4.0]}
        )

        values = store.read_machine_settings()
        assert values["reference_point.offsets.bottom_right"] == [-4.0, 4.0]

    def test_write_pair_keeps_table_comment(self, store: ConfigStore, config_dir: Path):
        store.write_machine_settings({"reference_point.offsets.top_left": [6.0, -6.0]})

        text = (config_dir / "machine.toml").read_text(encoding="utf-8")
        assert "[reference_point.offsets] # [x, y]で記述" in text
        assert "top_left = [6.0, -6.0]" in text

    @pytest.mark.parametrize(
        "value",
        [[1.0], [1.0, 2.0, 3.0], ["a", 1.0], [True, 1.0], 1.0, "1,2"],
    )
    def test_invalid_pair_raises(self, store: ConfigStore, value: object):
        # 型不一致の拒否を検証するため、意図的に契約外の値を渡す
        ill_typed = cast("MachineSettingValue", value)
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings(
                {"reference_point.offsets.top_left": ill_typed}
            )


class TestNozzleCapFields:
    """Nozzle_cap.x/y/z フィールドの読み書き（nozzle-cap-parking 計画書「WebUI」節）.

    Repo fixture には [nozzle_cap] を入れない（未記録が既定状態）ため、 欠落時は
    None、記録エンドポイント相当の write 後は round-trip する。
    """

    def test_missing_nozzle_cap_reads_as_none(self, store: ConfigStore):
        values = store.read_machine_settings()

        assert values["nozzle_cap.x"] is None
        assert values["nozzle_cap.y"] is None
        assert values["nozzle_cap.z"] is None

    def test_write_then_reread_round_trips(self, store: ConfigStore):
        store.write_machine_settings(
            {"nozzle_cap.x": 10.123, "nozzle_cap.y": 20.456, "nozzle_cap.z": 3.789},
        )

        values = store.read_machine_settings()
        assert values["nozzle_cap.x"] == 10.123
        assert values["nozzle_cap.y"] == 20.456
        assert values["nozzle_cap.z"] == 3.789


class TestPadAlignRegionSettings:
    """重複領域の寸法・overlap 設定を読み書きする."""

    def test_refinement_threshold_has_public_field_metadata(self):
        field = next(
            spec
            for spec in MACHINE_FIELDS
            if spec.key == "paste_dispenser.pad_align.refine_max_short_side"
        )

        assert field.label == "逐次位置合わせ対象の最大短辺"
        assert field.value_type == "float"
        assert field.unit == "mm"

    def test_missing_region_settings_read_as_none(self, store: ConfigStore):
        values = store.read_machine_settings()

        assert values["paste_dispenser.pad_align.region_size_px"] is None
        assert values["paste_dispenser.pad_align.region_overlap"] is None
        assert values["paste_dispenser.pad_align.refine_max_short_side"] is None

    @pytest.mark.parametrize("threshold", [0.0, 0.25])
    def test_write_refinement_threshold_then_reread_reflects_value(
        self, store: ConfigStore, threshold: float
    ):
        key = "paste_dispenser.pad_align.refine_max_short_side"

        store.write_machine_settings({key: threshold})

        assert store.read_machine_settings()[key] == pytest.approx(threshold)

    @pytest.mark.parametrize(
        "threshold",
        [True, -0.01, float("nan"), float("inf"), float("-inf")],
        ids=["bool", "negative", "nan", "positive-infinity", "negative-infinity"],
    )
    def test_invalid_refinement_threshold_raises(
        self, store: ConfigStore, threshold: MachineSettingValue
    ):
        with pytest.raises(UnknownFieldError, match="refine_max_short_side"):
            store.write_machine_settings(
                {"paste_dispenser.pad_align.refine_max_short_side": threshold}
            )

    def test_write_then_reread_reflects_values(self, store: ConfigStore):
        store.write_machine_settings(
            {
                "paste_dispenser.pad_align.region_size_px": 160,
                "paste_dispenser.pad_align.region_overlap": 0.25,
            }
        )

        values = store.read_machine_settings()
        assert values["paste_dispenser.pad_align.region_size_px"] == 160
        assert values["paste_dispenser.pad_align.region_overlap"] == pytest.approx(0.25)

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("paste_dispenser.pad_align.region_size_px", 0),
            ("paste_dispenser.pad_align.region_overlap", -0.01),
            ("paste_dispenser.pad_align.region_overlap", 1.0),
        ],
    )
    def test_invalid_values_raise(self, store: ConfigStore, key: str, value):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings({key: value})


class TestCopperPreprocessingSettings:
    """銅箔前処理の blur 設定を公開 ConfigStore 経由で検証する."""

    @pytest.mark.parametrize("blur_ksize", [0, -1, 2, 4])
    def test_rejects_blur_kernel_that_is_not_positive_and_odd(
        self, store: ConfigStore, blur_ksize: int
    ):
        with pytest.raises(UnknownFieldError, match="blur_ksize"):
            store.write_machine_settings(
                {"paste_dispenser.pad_align.blur_ksize": blur_ksize}
            )


class TestCameraCropFields:
    """Int 型フィールド camera.crop.width / camera.crop.height の 1 以上検証 （webui-
    camera-calib 計画書・要確認事項 2）.

    Change 即自動保存 UI では 0 や負値が machine.toml に書かれる事故が 起きやすいため、per-key
    検証を追加する。
    """

    @pytest.mark.parametrize("key", ["camera.crop.width", "camera.crop.height"])
    @pytest.mark.parametrize("value", [0, -1])
    def test_non_positive_value_raises(self, store: ConfigStore, key: str, value: int):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings({key: value})

    @pytest.mark.parametrize("key", ["camera.crop.width", "camera.crop.height"])
    def test_minimum_valid_value_is_accepted(self, store: ConfigStore, key: str):
        # 境界: 1 は有効な最小値
        store.write_machine_settings({key: 1})

        values = store.read_machine_settings()
        assert values[key] == 1


class TestAudioFields:
    """`[audio]` の読み書き（webui-audio-output 計画書「WebUI 配線」節）.

    Repo fixture には `[audio]` を入れない（未設定でも既定値で鳴るのが要件）ため、
    欠落時は None、write 後は round-trip する。device の空白のみ・volume の
    0..1 外は UnknownFieldError。
    """

    def test_missing_audio_reads_as_none(self, store: ConfigStore):
        values = store.read_machine_settings()

        assert values["audio.device"] is None
        assert values["audio.volume"] is None

    def test_write_then_reread_reflects_values(self, store: ConfigStore):
        store.write_machine_settings(
            {"audio.device": "  plughw:CARD=Audio,DEV=0  ", "audio.volume": 0.25}
        )

        values = store.read_machine_settings()
        assert values["audio.device"] == "plughw:CARD=Audio,DEV=0"
        assert values["audio.volume"] == 0.25

    @pytest.mark.parametrize("volume", [0.0, 1.0])
    def test_volume_boundaries_are_accepted(self, store: ConfigStore, volume: float):
        store.write_machine_settings({"audio.volume": volume})

        values = store.read_machine_settings()
        assert values["audio.volume"] == volume

    @pytest.mark.parametrize("volume", [float("nan"), -0.01, 1.01])
    def test_write_rejects_invalid_volume(self, store: ConfigStore, volume: float):
        with pytest.raises(UnknownFieldError, match="audio.volume"):
            store.write_machine_settings({"audio.volume": volume})

    def test_write_rejects_blank_device(self, store: ConfigStore):
        with pytest.raises(UnknownFieldError, match="audio.device"):
            store.write_machine_settings({"audio.device": "  "})

    def test_creating_audio_table_keeps_existing_comments(
        self, store: ConfigStore, config_dir: Path
    ):
        """`[audio]` テーブルの新規作成でも既存コメントは失われない."""
        store.write_machine_settings({"audio.volume": 0.5})

        text = (config_dir / "machine.toml").read_text(encoding="utf-8")
        assert "キャリブレーション値 2026/06/08" in text
        assert "[reference_point.offsets] # [x, y]で記述" in text
        assert "volume = 0.5" in text
