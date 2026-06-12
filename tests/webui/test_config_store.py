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

        assert values["paste_dispenser.fill_speed"] == 0.8
        assert values["paste_dispenser.toolhead.x"] == -1.772
        assert values["paste_dispenser.pad_align.blur_ksize"] == 5
        assert values["probe.servo_name"] == "probe_gnd"
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

    def test_write_then_reread_reflects_value(self, store: ConfigStore):
        store.write_machine_settings(FIXTURE, {"paste_dispenser.fill_speed": 0.9})

        values = store.read_machine_settings(FIXTURE)
        assert values["paste_dispenser.fill_speed"] == 0.9

    def test_write_keeps_untouched_lines_byte_identical(
        self, store: ConfigStore, configs_root: Path
    ):
        path = configs_root / FIXTURE / "machine.toml"
        before = path.read_text(encoding="utf-8").splitlines()

        store.write_machine_settings(FIXTURE, {"paste_dispenser.fill_speed": 0.9})

        after = path.read_text(encoding="utf-8").splitlines()
        assert len(after) == len(before)
        changed = [(b, a) for b, a in zip(before, after) if b != a]
        assert len(changed) == 1
        assert "fill_speed" in changed[0][0]

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

    def test_write_adds_whitelisted_key_missing_from_toml(self, store: ConfigStore):
        store.write_machine_settings(
            FIXTURE, {"paste_dispenser.bead_width_factor": 1.5}
        )

        values = store.read_machine_settings(FIXTURE)
        assert values["paste_dispenser.bead_width_factor"] == 1.5

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
                FIXTURE, {"paste_dispenser.fill_speed": "fast"}
            )

    def test_non_integral_float_for_int_field_raises(self, store: ConfigStore):
        with pytest.raises(UnknownFieldError):
            store.write_machine_settings(
                FIXTURE, {"paste_dispenser.pad_align.blur_ksize": 5.5}
            )

    def test_unknown_machine_raises_file_not_found(self, store: ConfigStore):
        with pytest.raises(FileNotFoundError):
            store.read_machine_settings("no-such-machine")
