"""`web.api.history.ConfigHistory` の仕様テスト.

- machine.toml の内容が変わる書き込みの前に、書き込む前の内容を世代として残す
- 世代は名前の時刻で新しい順に並べる（cp や unzip で置き直して mtime が変わっても並びは変わらない）
- 世代は新しいものから 50 個だけ残す。名前付きの世代（machine-pre-*）は消さない
- 復元はファイルをそのまま戻す（コメントも戻る）。復元する前の内容も世代に残す
"""

import os
from pathlib import Path

import pytest

from web.api.history import MAX_SNAPSHOTS, ConfigHistory, UnknownSnapshotError
from web.api.machine_config import MachineConfigFile


@pytest.fixture
def history(machine_config: MachineConfigFile) -> ConfigHistory:
    return ConfigHistory(machine_config.path)


class TestSnapshotOnWrite:
    def test_write_keeps_the_previous_content_with_its_change(
        self,
        machine_config: MachineConfigFile,
        history: ConfigHistory,
        config_dir: Path,
    ):
        original = machine_config.path.read_bytes()

        machine_config.write({"paste_dispenser.motion.lift_height": 3.0})

        [entry] = history.entries()
        assert (config_dir / "history" / entry.name).read_bytes() == original
        assert entry.changes == ["paste_dispenser.motion.lift_height: 2.0 → 3.0"]

    def test_entries_are_newest_first_and_each_shows_its_own_change(
        self, machine_config: MachineConfigFile, history: ConfigHistory
    ):
        machine_config.write({"paste_dispenser.motion.lift_height": 3.0})
        machine_config.write({"camera.fps": 20.0})

        assert [entry.changes for entry in history.entries()] == [
            ["camera.fps: 30.0 → 20.0"],
            ["paste_dispenser.motion.lift_height: 2.0 → 3.0"],
        ]

    def test_order_follows_the_name_not_the_mtime(
        self,
        machine_config: MachineConfigFile,
        history: ConfigHistory,
        config_dir: Path,
    ):
        machine_config.write({"paste_dispenser.motion.lift_height": 3.0})
        machine_config.write({"camera.fps": 20.0})
        # 置き直しで古い世代ほど mtime が新しくなった状態
        newest_first = sorted((config_dir / "history").iterdir(), reverse=True)
        for mtime, path in enumerate(newest_first):
            os.utime(path, (mtime, mtime))

        assert [entry.changes for entry in history.entries()] == [
            ["camera.fps: 30.0 → 20.0"],
            ["paste_dispenser.motion.lift_height: 2.0 → 3.0"],
        ]

    def test_write_without_change_keeps_no_snapshot(
        self, machine_config: MachineConfigFile, history: ConfigHistory
    ):
        machine_config.write({"paste_dispenser.motion.lift_height": 2.0})

        assert history.entries() == []

    def test_added_and_removed_keys_are_shown_as_none(
        self, machine_config: MachineConfigFile, history: ConfigHistory
    ):
        machine_config.write({"audio.volume": 0.5})

        [entry] = history.entries()
        assert entry.changes == ["audio.volume: （なし） → 0.5"]

    def test_unreadable_toml_is_reported_instead_of_a_diff(
        self, machine_config: MachineConfigFile, history: ConfigHistory
    ):
        machine_config.write({"paste_dispenser.motion.lift_height": 3.0})
        machine_config.path.write_text("[paste_dispenser\n")

        [entry] = history.entries()
        assert entry.changes == ["TOML として読めないため差分を出せません"]


class TestRotation:
    def test_keeps_the_newest_snapshots_and_every_named_one(
        self, history: ConfigHistory, config_dir: Path
    ):
        named = config_dir / "history" / "machine-pre-reorganize.toml"
        history.snapshot()
        named.write_text("# 移行前\n")
        oldest = {entry.name for entry in history.entries()} - {named.name}

        for _ in range(MAX_SNAPSHOTS):
            history.snapshot()

        names = {entry.name for entry in history.entries()}
        assert len(names) == MAX_SNAPSHOTS + 1
        assert named.name in names
        assert not oldest & names


class TestRestore:
    def test_restore_brings_back_the_file_and_keeps_the_replaced_content(
        self,
        machine_config: MachineConfigFile,
        history: ConfigHistory,
        config_dir: Path,
    ):
        path = machine_config.path
        original = path.read_bytes()
        machine_config.write({"paste_dispenser.motion.lift_height": 3.0})
        written = path.read_bytes()
        [entry] = history.entries()

        machine_config.restore(entry.name)

        # コメントを含めて元のファイルに戻る
        assert path.read_bytes() == original
        newest, restored = history.entries()
        assert (config_dir / "history" / newest.name).read_bytes() == written
        assert newest.changes == ["paste_dispenser.motion.lift_height: 3.0 → 2.0"]
        assert restored.name == entry.name

    @pytest.mark.parametrize("name", ["machine-missing.toml", "../machine.toml"])
    def test_unknown_name_changes_nothing(
        self, machine_config: MachineConfigFile, history: ConfigHistory, name: str
    ):
        machine_config.write({"paste_dispenser.motion.lift_height": 3.0})
        written = machine_config.path.read_bytes()

        with pytest.raises(UnknownSnapshotError):
            machine_config.restore(name)

        assert machine_config.path.read_bytes() == written
        assert len(history.entries()) == 1
