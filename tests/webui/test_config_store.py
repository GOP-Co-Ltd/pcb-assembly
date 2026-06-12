"""`webui.config_store.ConfigStore` の仕様テスト.

計画書「`src/webui/config_store.py`」節が契約:

- machine.toml / printer.cfg のホワイトリスト読み書き
- tomlkit によるコメント・構造保持（変更対象外の行は不変）
- printer.cfg は既存行のみ書き換え（行追加しない）
- 未知キー / 型不一致 / 対象行なし → UnknownFieldError
"""

from pathlib import Path

import pytest

from webui.config_store import (
    MACHINE_FIELDS,
    MOTION_FIELDS,
    ConfigStore,
    UnknownFieldError,
)

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


class TestMotionSettings:
    """printer.cfg のホワイトリスト読み書き（既存行のみ）."""

    def test_read_returns_motion_values(self, store: ConfigStore):
        values = store.read_motion_settings(FIXTURE)

        assert values["printer.max_velocity"] == 50.0
        assert values["printer.max_accel"] == 500.0
        assert values["manual_stepper paste_dispenser.velocity"] == 1.0
        assert values["manual_stepper paste_dispenser.accel"] == 10.0

    def test_read_covers_every_whitelisted_key(self, store: ConfigStore):
        values = store.read_motion_settings(FIXTURE)

        assert set(values) == {spec.key for spec in MOTION_FIELDS}

    def test_write_rewrites_only_target_line(
        self, store: ConfigStore, configs_root: Path
    ):
        path = configs_root / FIXTURE / "printer.cfg"
        before = path.read_text(encoding="utf-8").splitlines()

        store.write_motion_settings(FIXTURE, {"printer.max_velocity": 45.0})

        after = path.read_text(encoding="utf-8").splitlines()
        assert len(after) == len(before)
        changed = [(b, a) for b, a in zip(before, after) if b != a]
        assert len(changed) == 1
        assert "max_velocity" in changed[0][0]
        assert store.read_motion_settings(FIXTURE)["printer.max_velocity"] == 45.0

    def test_write_when_option_line_missing_raises(
        self, store: ConfigStore, configs_root: Path
    ):
        path = configs_root / FIXTURE / "printer.cfg"
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        path.write_text(
            "".join(line for line in lines if not line.startswith("max_accel")),
            encoding="utf-8",
        )

        with pytest.raises(UnknownFieldError):
            store.write_motion_settings(FIXTURE, {"printer.max_accel": 600.0})

    def test_unknown_key_raises_unknown_field_error(self, store: ConfigStore):
        with pytest.raises(UnknownFieldError):
            store.write_motion_settings(FIXTURE, {"printer.no_such_option": 1.0})


class TestSymlinkPointsTo:
    """printer_cfg_link の検証."""

    def test_true_when_link_targets_machine_printer_cfg(
        self, store: ConfigStore, configs_root: Path, tmp_path: Path
    ):
        link = tmp_path / "printer_data" / "config" / "printer.cfg"
        link.parent.mkdir(parents=True)
        link.symlink_to(configs_root / FIXTURE / "printer.cfg")

        assert store.symlink_points_to(FIXTURE, link) is True

    def test_false_when_link_targets_other_machine(
        self, store: ConfigStore, configs_root: Path, tmp_path: Path
    ):
        link = tmp_path / "printer_data" / "config" / "printer.cfg"
        link.parent.mkdir(parents=True)
        link.symlink_to(configs_root / "kurousagi" / "printer.cfg")

        assert store.symlink_points_to(FIXTURE, link) is False

    def test_false_when_link_does_not_exist(self, store: ConfigStore, tmp_path: Path):
        assert store.symlink_points_to(FIXTURE, tmp_path / "no-such-link") is False
