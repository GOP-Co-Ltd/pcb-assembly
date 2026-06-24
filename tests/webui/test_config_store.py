"""`webui.config_store.ConfigStore` の仕様テスト.

計画書「`src/webui/config_store.py`」節が契約:

- machine.toml のホワイトリスト読み書き
- tomlkit によるコメント・構造保持（変更対象外の行は不変）
- 未知キー / 型不一致 → UnknownFieldError
"""

from pathlib import Path

import pytest

from webui.config_store import MACHINE_FIELDS, ConfigStore, UnknownFieldError

FIXTURE = "test-fixture"


@pytest.fixture
def store(configs_root: Path) -> ConfigStore:
    return ConfigStore(configs_root)


class TestListMachines:
    """マシン列挙."""

    def test_lists_only_directories_with_machine_toml_sorted(
        self, store: ConfigStore, configs_root: Path
    ):
        (configs_root / "no-machine-toml").mkdir()

        assert store.list_machines() == ["kurousagi", "test-fixture"]


class TestMachineSettings:
    """machine.toml のホワイトリスト読み書き."""

    def test_read_returns_values_with_declared_types(self, store: ConfigStore):
        values = store.read_machine_settings(FIXTURE)

        assert values["paste_dispenser.dispense_mode"] == "auto"
        assert values["paste_dispenser.auto_line_aspect_ratio"] == 1.618
        assert values["paste_dispenser.max_fill_speed"] == 0.8
        assert values["paste_dispenser.solder_paste_density"] == 3.78
        assert values["paste_dispenser.paste_height"] == "auto"
        assert values["paste_dispenser.toolhead.x"] == -1.772
        assert values["paste_dispenser.pad_align.blur_ksize"] == 5
        assert values["probe.servo_name"] == "probe_gnd"
        assert values["probe.shift"] == [-0.5, 0.0]
        assert values["camera.device_id"] == 0
        assert values["camera.format"] == "YUYV"
        assert values["camera.crop.width"] == 600
        assert values["reference_point.target_diameter"] == 3.0

    def test_read_covers_every_whitelisted_key(self, store: ConfigStore):
        values = store.read_machine_settings(FIXTURE)

        assert set(values) == {spec.key for spec in MACHINE_FIELDS}

    def test_missing_keys_read_as_none(self, store: ConfigStore):
        values = store.read_machine_settings(FIXTURE)

        assert values["paste_dispenser.bead_width_factor"] is None
        assert values["paste_dispenser.boundary_margin"] is None
        assert values["probe.lift_height"] is None

    def test_write_then_reread_reflects_value(self, store: ConfigStore):
        store.write_machine_settings(FIXTURE, {"paste_dispenser.max_fill_speed": 0.9})

        values = store.read_machine_settings(FIXTURE)
        assert values["paste_dispenser.max_fill_speed"] == 0.9

    def test_write_dispense_mode_then_reread_reflects_value(self, store: ConfigStore):
        store.write_machine_settings(FIXTURE, {"paste_dispenser.dispense_mode": "line"})

        values = store.read_machine_settings(FIXTURE)
        assert values["paste_dispenser.dispense_mode"] == "line"

    def test_write_solder_paste_density_then_reread_reflects_value(
        self, store: ConfigStore
    ):
        store.write_machine_settings(
            FIXTURE, {"paste_dispenser.solder_paste_density": 4.1}
        )

        values = store.read_machine_settings(FIXTURE)
        assert values["paste_dispenser.solder_paste_density"] == 4.1

    def test_write_auto_paste_height_then_reread_reflects_value(
        self, store: ConfigStore
    ):
        store.write_machine_settings(FIXTURE, {"paste_dispenser.paste_height": 0.25})
        store.write_machine_settings(FIXTURE, {"paste_dispenser.paste_height": "auto"})

        values = store.read_machine_settings(FIXTURE)
        assert values["paste_dispenser.paste_height"] == "auto"

    def test_write_keeps_untouched_lines_byte_identical(
        self, store: ConfigStore, configs_root: Path
    ):
        path = configs_root / FIXTURE / "machine.toml"
        before = path.read_text(encoding="utf-8").splitlines()

        store.write_machine_settings(FIXTURE, {"paste_dispenser.max_fill_speed": 0.9})

        after = path.read_text(encoding="utf-8").splitlines()
        assert len(after) == len(before)
        changed = [(b, a) for b, a in zip(before, after) if b != a]
        assert len(changed) == 1
        assert "max_fill_speed" in changed[0][0]

    def test_write_keeps_inline_comment_on_changed_line(
        self, store: ConfigStore, configs_root: Path
    ):
        store.write_machine_settings(FIXTURE, {"probe.down_distance": 2.5})

        path = configs_root / FIXTURE / "machine.toml"
        line = next(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("down_distance")
        )
        assert "2.5" in line
        assert "グラウンドを下げる距離" in line

    def test_write_probe_shift_changes_array_value(
        self, store: ConfigStore, configs_root: Path
    ):
        store.write_machine_settings(FIXTURE, {"probe.shift": [0.25, -0.75]})

        values = store.read_machine_settings(FIXTURE)
        assert values["probe.shift"] == [0.25, -0.75]
        path = configs_root / FIXTURE / "machine.toml"
        line = next(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("shift")
        )
        assert "[0.25, -0.75]" in line
        assert "プローブ点のシフト量" in line

    def test_write_adds_whitelisted_key_missing_from_toml(self, store: ConfigStore):
        store.write_machine_settings(
            FIXTURE, {"paste_dispenser.bead_width_factor": 1.5}
        )

        values = store.read_machine_settings(FIXTURE)
        assert values["paste_dispenser.bead_width_factor"] == 1.5

    def test_write_adds_probe_lift_height_missing_from_toml(
        self, store: ConfigStore, configs_root: Path
    ):
        store.write_machine_settings(FIXTURE, {"probe.lift_height": 1.25})

        values = store.read_machine_settings(FIXTURE)
        assert values["probe.lift_height"] == 1.25
        text = (configs_root / FIXTURE / "machine.toml").read_text(encoding="utf-8")
        assert "lift_height = 1.25" in text

    def test_read_includes_camera_calibration_file(self, store: ConfigStore):
        """Phase 4: camera.calibration_file がホワイトリストに含まれ既存値が読める."""
        values = store.read_machine_settings(FIXTURE)

        assert values["camera.calibration_file"] == "ov9281_test_fixture.json"

    def test_write_calibration_file_changes_only_target_line(
        self, store: ConfigStore, configs_root: Path
    ):
        """camera.calibration_file の書込は対象行のみ変更しコメント・構造を保つ."""
        path = configs_root / FIXTURE / "machine.toml"
        before = path.read_text(encoding="utf-8").splitlines()

        store.write_machine_settings(
            FIXTURE, {"camera.calibration_file": "ov9281_20260612.json"}
        )

        values = store.read_machine_settings(FIXTURE)
        assert values["camera.calibration_file"] == "ov9281_20260612.json"
        after = path.read_text(encoding="utf-8").splitlines()
        assert len(after) == len(before)
        changed = [(b, a) for b, a in zip(before, after) if b != a]
        assert len(changed) == 1
        assert "calibration_file" in changed[0][0]

    def test_non_string_calibration_file_raises(self, store: ConfigStore):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings(FIXTURE, {"camera.calibration_file": 1.0})

    def test_unknown_key_raises_unknown_field_error(self, store: ConfigStore):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings(FIXTURE, {"paste_dispenser.no_such_key": 1.0})

    def test_type_mismatch_raises_unknown_field_error(self, store: ConfigStore):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings(
                FIXTURE, {"paste_dispenser.max_fill_speed": "fast"}
            )

    def test_unknown_dispense_mode_raises_unknown_field_error(self, store: ConfigStore):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings(
                FIXTURE, {"paste_dispenser.dispense_mode": "spray"}
            )

    def test_auto_line_aspect_ratio_must_exceed_one(self, store: ConfigStore):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings(
                FIXTURE, {"paste_dispenser.auto_line_aspect_ratio": 1.0}
            )

    def test_manual_paste_height_must_be_positive(self, store: ConfigStore):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings(FIXTURE, {"paste_dispenser.paste_height": 0.0})

    def test_solder_paste_density_must_be_positive(self, store: ConfigStore):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings(
                FIXTURE, {"paste_dispenser.solder_paste_density": 0.0}
            )

    def test_non_integral_float_for_int_field_raises(self, store: ConfigStore):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings(
                FIXTURE, {"paste_dispenser.pad_align.blur_ksize": 5.5}
            )

    def test_invalid_float_pair_raises_unknown_field_error(self, store: ConfigStore):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings(FIXTURE, {"probe.shift": [1.0, 2.0, 3.0]})

    def test_unknown_machine_raises_file_not_found(self, store: ConfigStore):
        with pytest.raises(FileNotFoundError):
            store.read_machine_settings("no-such-machine")


class TestAirPumpEnabled:
    """Bool 型フィールド air_pump_enabled の読み書き."""

    def test_missing_air_pump_enabled_reads_as_none(self, store: ConfigStore):
        values = store.read_machine_settings(FIXTURE)

        assert values["paste_dispenser.air_pump_enabled"] is None

    @pytest.mark.parametrize("enabled", [True, False])
    def test_write_then_reread_reflects_bool(self, store: ConfigStore, enabled: bool):
        store.write_machine_settings(
            FIXTURE, {"paste_dispenser.air_pump_enabled": enabled}
        )

        values = store.read_machine_settings(FIXTURE)
        assert values["paste_dispenser.air_pump_enabled"] is enabled

    def test_bool_field_rejects_non_bool(self, store: ConfigStore):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings(
                FIXTURE, {"paste_dispenser.air_pump_enabled": 1.0}
            )

    def test_numeric_field_still_rejects_bool(self, store: ConfigStore):
        # bool は int のサブクラスなので、数値フィールドへの bool 投入は拒否され続ける
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings(
                FIXTURE, {"paste_dispenser.max_fill_speed": True}
            )
