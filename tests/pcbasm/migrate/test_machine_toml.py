"""machine.toml の移行の仕様.

移行 M は死にキーを除いて節を再編し、移行 N は [paste_dispenser] と [tact] を [solder]
の階層へ作り直す。 002 の旧形（M の前）も 001 の live（M の後）も、続けて当てて同じ形になる。
"""

import shutil
import subprocess
import sys
import tomllib
from operator import attrgetter
from pathlib import Path

import pytest

from pcbasm.config import load_machine
from pcbasm.migrate.machine_toml import (
    MigrationError,
    migrate_machine_toml,
    migrate_text,
)
from tests.support.paths import TEST_CONFIG_DIR

# 002 の旧形（移行 M の前）と、001 の live（移行 M の後、N の前）
BEFORE_M = Path(__file__).with_name("machine-before-M.toml")
BEFORE_N = Path(__file__).with_name("machine-before-N.toml")

# コードが読んでいなかったので引き継がないキー
DEAD = {
    "machine_type",
    "xy_calibration.max_standard_deviation",
    "paste_dispenser.air_pump_enabled",
    "paste_dispenser.pad_align.tolerance",
    "paste_dispenser.pad_align.roi_margin",
    "paste_dispenser.pad_align.min_roi",
    "paste_dispenser.pad_align.region_count",
    "paste_dispenser.pad_align.min_regions",
    # 所要時間の予測は printer.cfg の速度と加速度を読む
    "tact.travel_speed",
    "tact.travel_accel",
    "tact.z_speed",
    "tact.z_accel",
}
DEFAULTS = {
    "dispense_mode",
    "line_direction",
    "ul_per_mm2",
    "paste_height",
    "prime_extra_delay",
    "bead_width_factor",
    "overlap",
    "boundary_margin",
    "micro_line_z_start",
    "micro_line_z_end",
}
MOTION = {
    "max_fill_speed",
    "max_dispense_rate",
    "dispense_accel",
    "retract_amount",
    "retract_rate",
    "retract_accel_factor",
    "lift_height",
    "micro_line_settle_seconds",
    "post_dispense_settle_seconds",
}


# 移行 N: [paste_dispenser] と [tact] のキー → [solder] の下のキー
SOLDER = {
    "paste_dispenser.solder_paste_density": "solder.paste_density",
    "paste_dispenser.initial_purge_ul": "solder.initial_purge_ul",
    "paste_dispenser.rotations_per_ul": "solder.dispenser.rotations_per_ul",
    "paste_dispenser.motion.max_dispense_rate": "solder.dispenser.max_dispense_rate",
    "paste_dispenser.motion.dispense_accel": "solder.dispenser.dispense_accel",
    "paste_dispenser.motion.retract_amount": "solder.dispenser.retract_amount",
    "paste_dispenser.motion.retract_rate": "solder.dispenser.retract_rate",
    "paste_dispenser.motion.retract_accel_factor": (
        "solder.dispenser.retract_accel_factor"
    ),
    "paste_dispenser.nozzle_diameter": "solder.nozzle.diameter",
    "paste_dispenser.nozzle_outer_diameter": "solder.nozzle.outer_diameter",
    "paste_dispenser.toolhead.": "solder.nozzle.offset.",
    "paste_dispenser.nozzle_cap.": "solder.nozzle.cap.",
    "paste_dispenser.nozzle_clean.": "solder.nozzle.clean.",
    "paste_dispenser.motion.": "solder.motion.",
    "paste_dispenser.defaults.": "solder.defaults.",
    "paste_dispenser.auto_line_aspect_ratio": "solder.auto.line_aspect_ratio",
    "paste_dispenser.auto_area_short_side_factor": (
        "solder.auto.area_short_side_factor"
    ),
    "paste_dispenser.volume_correction.": "solder.volume_correction.",
    "tact.": "solder.tact.",
}


def _path_after_m(path: str) -> str:
    """移行 M の前のキーのパス → M の後のパス."""
    section, _, rest = path.partition(".")
    if section != "paste_dispenser":
        return path
    if rest.startswith("pad_align."):
        return "alignment." + rest.removeprefix("pad_align.")
    if rest.startswith("flow_calibration."):
        return "paste_dispenser.volume_correction." + rest.removeprefix(
            "flow_calibration."
        )
    if rest in DEFAULTS:
        return f"paste_dispenser.defaults.{rest}"
    if rest in MOTION:
        return f"paste_dispenser.motion.{rest}"
    return path


def _path_after_n(path: str) -> str:
    """移行 N の前のキーのパス → N の後のパス（表ごと移すものは接頭辞で引く）."""
    if path in SOLDER:
        return SOLDER[path]
    prefix = next((p for p in SOLDER if p.endswith(".") and path.startswith(p)), None)
    return path if prefix is None else SOLDER[prefix] + path.removeprefix(prefix)


def _new_path(path: str, source: Path) -> str:
    """``source`` の形のキーのパス → 移行後のパス."""
    if source == BEFORE_M:
        path = _path_after_m(path)
    return _path_after_n(path)


def _attribute_path(path: str) -> str:
    """TOML のパス → MachineConfig の属性パス（ノズル位置の x / y / z は position の 1 組）."""
    table, _, key = path.rpartition(".")
    if table.endswith(("nozzle.cap", "nozzle.clean")) and key in ("x", "y", "z"):
        return f"{table}.position.{key}"
    return path


def _leaves(table: dict, prefix: str = "") -> dict[str, object]:
    leaves: dict[str, object] = {}
    for key, value in table.items():
        if isinstance(value, dict):
            leaves.update(_leaves(value, f"{prefix}{key}."))
        else:
            leaves[f"{prefix}{key}"] = value
    return leaves


@pytest.fixture(params=[BEFORE_M, BEFORE_N], ids=["002-before-M", "001-before-N"])
def source(request: pytest.FixtureRequest) -> Path:
    return request.param


@pytest.fixture
def machine_toml(tmp_path: Path, source: Path) -> Path:
    path = tmp_path / "machine.toml"
    shutil.copy(source, path)
    return path


class TestMigrateMachineToml:
    def test_keeps_every_live_value_at_its_new_path(
        self, machine_toml: Path, source: Path
    ):
        before = _leaves(tomllib.loads(machine_toml.read_text()))

        migrate_machine_toml(machine_toml)

        after = _leaves(tomllib.loads(machine_toml.read_text()))
        expected = {
            _new_path(path, source): value
            for path, value in before.items()
            if path not in DEAD
        }
        assert after == expected

    def test_loaded_values_equal_the_values_before_migration(
        self, machine_toml: Path, source: Path
    ):
        before = _leaves(tomllib.loads(machine_toml.read_text()))

        migrate_machine_toml(machine_toml)

        machine = load_machine(machine_toml)
        for path, value in before.items():
            # 校正ファイルは設定ディレクトリからの絶対パスに解決される
            if path in DEAD or path == "camera.calibration_file":
                continue
            actual = attrgetter(_attribute_path(_new_path(path, source)))(machine)
            assert (
                list(actual) if isinstance(actual, tuple) else actual
            ) == value, path

    def test_both_forms_end_in_the_same_solder_layout(self):
        """002 の旧形と 001 の live は、同じ [solder] の階層になる."""
        migrated = [
            tomllib.loads(migrate_text(path.read_text())[0])
            for path in (BEFORE_M, BEFORE_N)
        ]

        assert migrated[0] == migrated[1]
        assert "paste_dispenser" not in migrated[0] and "tact" not in migrated[0]
        assert list(migrated[0]["solder"]) == [
            "paste_density",
            "initial_purge_ul",
            "dispenser",
            "nozzle",
            "motion",
            "defaults",
            "auto",
            "volume_correction",
            "tact",
        ]
        assert list(migrated[0]["solder"]["nozzle"]) == [
            "diameter",
            "outer_diameter",
            "offset",
            "cap",
            "clean",
        ]

    def test_is_idempotent(self, machine_toml: Path):
        migrate_machine_toml(machine_toml)
        migrated = machine_toml.read_text()

        assert migrate_machine_toml(machine_toml) == {}
        assert machine_toml.read_text() == migrated

    def test_keeps_comments(self, machine_toml: Path):
        migrate_machine_toml(machine_toml)

        text = machine_toml.read_text()
        motion = text.split("[solder.motion]")[1].split("\n[")[0]
        assert (
            "max_fill_speed = 2.0 # 連続塗布できる移動速度上限 [mm/sec]"
            "（実機で 1812 が連続になるまで調整）"
        ) in motion
        # 単独の行のコメントは直前のキー（最初のキーより前なら最初のキー）と一緒に動く
        dispenser = text.split("[solder.dispenser]")[1].split("\n[")[0]
        assert dispenser.startswith(
            "\n# rotations_per_ul = 1.0 # 1μLあたりの回転数 [rev/μL]\n"
            "rotations_per_ul = 15.0 # キャリブレーション値 2026/06/08 23G S3X70-E150DN\n"
            "# rotations_per_ul = 31.491713 # キャリブレーション値 2026/06/05 S3X70-E150DN\n"
        )
        alignment = text.split("[alignment]")[1]
        assert alignment.startswith("\n# pad単位の銅箔照合による位置合わせ\n")
        # 次の表の見出しの前に書いたコメントは、その見出しの前に残る
        assert (
            "# タスク終了時の駐機先。WebUI の「ノズル位置の設定」で記録する。\n"
            "[solder.nozzle.cap]"
        ) in text

    def test_snapshots_the_original_before_each_step(self, tmp_path: Path):
        """当てたステップごとに、その手前の内容を残す（002 は M と N、001 は N だけ）."""
        path = tmp_path / "machine.toml"
        shutil.copy(BEFORE_M, path)
        original = path.read_text()
        after_m = BEFORE_N.read_text().split("\n", 2)[2]

        migrate_machine_toml(path)

        history = tmp_path / "history"
        assert (history / "machine-pre-M.toml").read_text() == original
        assert (history / "machine-pre-N.toml").read_text().endswith(after_m)

    def test_snapshots_only_the_steps_it_applies(self, tmp_path: Path):
        path = tmp_path / "machine.toml"
        shutil.copy(BEFORE_N, path)

        migrate_machine_toml(path)

        history = tmp_path / "history"
        assert (history / "machine-pre-N.toml").read_text() == BEFORE_N.read_text()
        assert not (history / "machine-pre-M.toml").exists()

    def test_keeps_the_first_snapshot(self, machine_toml: Path):
        """古い世代を復元して 2 回目の移行が当たっても、最初の移行の戻り先を残す."""
        snapshot = machine_toml.parent / "history" / "machine-pre-N.toml"
        snapshot.parent.mkdir()
        snapshot.write_text("# 最初の移行の前\n")

        migrate_machine_toml(machine_toml)

        assert snapshot.read_text() == "# 最初の移行の前\n"

    def test_ends_with_a_single_newline(self, machine_toml: Path):
        migrate_machine_toml(machine_toml)

        text = machine_toml.read_text()
        assert text.endswith("\n") and not text.endswith("\n\n")

    def test_keeps_only_setup_sec_in_tact(self, machine_toml: Path):
        """速度と加速度は printer.cfg から読むので消し、setup_sec はコメントごと [solder.tact] へ移す."""
        migrate_machine_toml(machine_toml)

        text = machine_toml.read_text()
        assert tomllib.loads(text)["solder"]["tact"] == {"setup_sec": 300.0}
        tact = text.split("[solder.tact]")[1]
        assert tact == (
            "\n# タクトタイム見積り専用の値（装置の動作はこれを参照しない）\n"
            "setup_sec = 300.0 # 位置合わせ・高さ計測・パージ等の固定分 [sec]（実測に合わせる）\n"
        )

    def test_drops_tact_speeds_from_a_file_already_in_the_current_layout(self):
        """移行 M を当てた後に残っていた [tact] の速度も次の起動で消し、書き忘れた接続先は補わない."""
        text = (
            "[klipper]\nport = 7125\n\n"
            "[tact]\ntravel_speed = 10.0\nz_accel = 10.0\nsetup_sec = 200.0\n"
        )

        migrated, changes = migrate_text(text)

        assert migrated == "[klipper]\nport = 7125\n\n[tact]\nsetup_sec = 200.0\n"
        assert changes == {"M": ["tact.travel_speed を削除", "tact.z_accel を削除"]}
        assert migrate_text(migrated) == (migrated, {})

    def test_writes_the_former_klipper_defaults(self):
        """接続先の既定値は撤去したので、移行するファイルに書かれていなければこれまでの既定値を明示する."""
        migrated, changes = migrate_text(
            'machine_type = "paste"\n\n[klipper]\nport = 7125\n'
        )

        assert tomllib.loads(migrated) == {
            "klipper": {"host": "localhost", "port": 7125}
        }
        assert changes == {
            "M": ["machine_type を削除", "klipper.host = 'localhost' を明示"]
        }

    @pytest.mark.parametrize(
        "text",
        ["[klipper]\nport = 7125\n", "[camera]\ndevice_id = 0\n"],
        ids=["no-host", "no-klipper"],
    )
    def test_does_not_fill_in_klipper_of_the_current_layout(self, text: str):
        """新しい形のファイルで書き忘れた接続先は、補わずに設定エラーのままにする."""
        assert migrate_text(text) == (text, {})

    def test_current_layout_needs_no_migration(self):
        text = (TEST_CONFIG_DIR / "machine.toml").read_text()

        assert migrate_text(text) == (text, {})

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ('[klipper\nhost = "x"\n', "TOML として読めません"),
            (
                "[paste_dispenser]\noverlap = 0.2\n\n"
                "[paste_dispenser.defaults]\nul_per_mm2 = 0.2\n",
                "途中まで移行済み",
            ),
            (
                "[paste_dispenser.pad_align]\ncanny_low = 1.0\n\n[alignment]\n",
                "途中まで移行済み",
            ),
            (
                "[paste_dispenser]\noverlap = 0.2\n\n[camera]\n\n"
                "[paste_dispenser.pad_align]\ncanny_low = 1.0\n",
                "ほかの節をはさんで",
            ),
            (
                "[paste_dispenser]\nrotations_per_ul = 1.0\n\n"
                "[solder]\npaste_density = 3.78\n",
                "途中まで移行済み",
            ),
            (
                "[paste_dispenser]\nrotations_per_ul = 1.0\n\n[camera]\n\n"
                "[paste_dispenser.toolhead]\nx = 1.0\n",
                "ほかの節をはさんで",
            ),
        ],
        ids=[
            "broken",
            "partially-migrated-defaults",
            "partially-migrated-alignment",
            "interleaved",
            "partially-migrated-solder",
            "interleaved-solder",
        ],
    )
    def test_unmigratable_file_is_left_untouched(
        self, tmp_path: Path, text: str, message: str
    ):
        path = tmp_path / "machine.toml"
        path.write_text(text)

        with pytest.raises(MigrationError, match=message):
            migrate_machine_toml(path)

        assert path.read_text() == text
        assert not (tmp_path / "history").exists()


class TestCheckCommand:
    """``python -m pcbasm.migrate --check PATH`` は差分を表示するだけで書き換えない."""

    def test_prints_the_diff_without_writing(self, machine_toml: Path):
        original = machine_toml.read_text()

        result = subprocess.run(
            [sys.executable, "-m", "pcbasm.migrate", "--check", str(machine_toml)],
            capture_output=True,
            text=True,
            check=True,
        )

        assert "+[solder.motion]" in result.stdout
        assert "-[paste_dispenser]" in result.stdout
        assert machine_toml.read_text() == original
        assert not (machine_toml.parent / "history").exists()
