"""`pcbasm.commissioning` の仕様テスト.

立ち上げの各段は、設定キーと参照ファイルがそろえば完了とする（日付は見ない）。
未完了の段は、先に済ませる段が残っていれば「前提待ち」、そうでなければ残っていることを示す。
手順の並びは、文書（docs/commissioning.md）の表と同じにする。
"""

import re
from pathlib import Path

import attrs
import pytest

from pcbasm.commissioning import STEPS, readiness
from pcbasm.config import MachineConfig, load_machine
from pcbasm.config.solder import NozzleConfig
from pcbasm.geometry import Point3d
from pcbasm.machine.diagnostics import Check, save_checks
from pcbasm.paths import PROJECT_ROOT
from tests.support.paths import TEST_CONFIG_DIR

# カメラ校正（焦点 Z あり）は済み、ノズル位置と運転時補正の校正ファイルは未設定
MACHINE = load_machine(TEST_CONFIG_DIR / "machine.toml")
CALIBRATION = "s3x70.paste-volume.json"
ALL_OK = (Check("Klipper の接続", True, "localhost:7125"),)


def _taught(
    nozzle: NozzleConfig, cap: Point3d | None, clean: Point3d | None
) -> NozzleConfig:
    """キャップ位置とクリーニング位置を教示したノズル."""
    return attrs.evolve(
        nozzle,
        cap=attrs.evolve(nozzle.cap, position=cap),
        clean=attrs.evolve(nozzle.clean, position=clean),
    )


def _commissioned(machine: MachineConfig) -> MachineConfig:
    """ノズル位置を教示し、運転時補正の校正ファイルを選んだ machine."""
    solder = machine.solder
    return attrs.evolve(
        machine,
        solder=attrs.evolve(
            solder,
            nozzle=_taught(solder.nozzle, Point3d(1, 2, 3), Point3d(4, 5, 6)),
            volume_correction=attrs.evolve(
                solder.volume_correction, calibration_file=CALIBRATION
            ),
        ),
    )


@pytest.fixture
def calibration_dir(tmp_path: Path) -> Path:
    (tmp_path / CALIBRATION).write_text("{}")
    return tmp_path


@pytest.fixture
def all_ok(tmp_path: Path) -> Path:
    """全項目 ok の装置診断の結果."""
    path = tmp_path / "machine_check.json"
    save_checks(path, ALL_OK)
    return path


def _states(result) -> dict[str, tuple[str, str]]:
    return {step.name: (step.state, step.detail) for step in result.steps}


class TestSteps:
    def test_names_are_unique(self):
        names = [step.name for step in STEPS]

        assert len(names) == len(set(names))

    def test_requires_only_earlier_steps(self):
        """表の順に済ませれば、前提はいつも先に済んでいる."""
        earlier: set[str] = set()
        for step in STEPS:
            assert set(step.requires) <= earlier, step.name
            earlier.add(step.name)

    def test_docs_table_lists_the_steps_in_the_same_order(self):
        text = (PROJECT_ROOT / "docs" / "commissioning.md").read_text()
        rows = re.findall(r"^\| \d+ +\| (.+?) +\| `(\w+)` +\|", text, re.MULTILINE)

        assert rows == [(step.label, step.name) for step in STEPS]


class TestReadiness:
    def test_all_steps_done(self, all_ok: Path, calibration_dir: Path):
        result = readiness(_commissioned(MACHINE), all_ok, calibration_dir)

        assert [step.state for step in result.steps] == ["done"] * len(STEPS)
        assert result.complete
        assert result.label == "立ち上げ済み"

    def test_done_step_shows_its_condition(self, all_ok: Path, calibration_dir: Path):
        result = readiness(_commissioned(MACHINE), all_ok, calibration_dir)

        step = result.steps[0]
        assert (step.name, step.state_label) == ("camera_calibration", "完了")
        assert step.detail == STEPS[0].done_when
        assert step.redo_when == STEPS[0].redo_when

    def test_new_machine(self, tmp_path: Path):
        """テンプレートどおりの machine.toml で、ノズルを教示しておらず、診断もしていない."""
        result = readiness(MACHINE, tmp_path / "machine_check.json", tmp_path)

        assert _states(result) == {
            "camera_calibration": ("done", STEPS[0].done_when),
            "machine_check": ("todo", "まだ診断していません"),
            "reference_point_setup": ("done", STEPS[2].done_when),
            "nozzle_cap": ("blocked", "先に「装置診断」を済ませてください"),
            "toolhead_offset": ("done", STEPS[4].done_when),
            "dispense_calibration": ("done", STEPS[5].done_when),
            "paste_volume_calibration": (
                "todo",
                "運転時補正に使う校正ファイルを選んでいません",
            ),
        }
        assert not result.complete
        assert result.label == "立ち上げ 4/7"

    @pytest.mark.parametrize(
        ("cap", "clean", "detail"),
        [
            (None, None, "キャップ位置とクリーニング位置を記録していません"),
            (Point3d(1, 2, 3), None, "クリーニング位置を記録していません"),
            (None, Point3d(1, 2, 3), "キャップ位置を記録していません"),
        ],
    )
    def test_untaught_nozzle_positions(
        self,
        all_ok: Path,
        calibration_dir: Path,
        cap: Point3d | None,
        clean: Point3d | None,
        detail: str,
    ):
        machine = _commissioned(MACHINE)
        solder = machine.solder
        machine = attrs.evolve(
            machine,
            solder=attrs.evolve(solder, nozzle=_taught(solder.nozzle, cap, clean)),
        )

        result = readiness(machine, all_ok, calibration_dir)

        assert _states(result)["nozzle_cap"] == ("todo", detail)

    def test_failed_machine_check_names_the_failed_items(
        self, tmp_path: Path, calibration_dir: Path
    ):
        checks = (
            Check("Klipper の接続", True, "localhost:7125"),
            Check("カメラと校正", False, "カメラ 640x480 が校正 1280x720 と違います"),
            Check("通知音（ALSA）", False, "ALSA の再生デバイスが見つかりません"),
        )
        checks_file = tmp_path / "machine_check.json"
        save_checks(checks_file, checks)

        result = readiness(_commissioned(MACHINE), checks_file, calibration_dir)

        assert _states(result)["machine_check"] == (
            "todo",
            "直近の診断で不合格: カメラと校正、通知音（ALSA）",
        )

    def test_selected_volume_calibration_must_exist(self, all_ok: Path, tmp_path: Path):
        result = readiness(_commissioned(MACHINE), all_ok, tmp_path)

        assert _states(result)["paste_volume_calibration"] == (
            "todo",
            f"校正ファイル {CALIBRATION} がありません",
        )

    def test_camera_calibration_without_focus_z(
        self, tmp_path: Path, calibration_dir: Path
    ):
        """焦点 Z の無い校正ではジョブが止まるので、校正し直すまで未完了."""
        camera_file = tmp_path / "old.json"
        camera_file.write_text(
            '{"pixel_per_mm": 40.0, "square_size_mm": 1.5, "mean_distance_px": 60.0,'
            ' "std_distance_px": 0.25, "resolution": [1280, 720], "crop_size": [600, 600],'
            ' "calibrated_at": "2026-06-12T00:00:00"}'
        )
        machine = _commissioned(MACHINE)
        machine = attrs.evolve(
            machine, camera=attrs.evolve(machine.camera, calibration_file=camera_file)
        )

        result = readiness(machine, tmp_path / "machine_check.json", calibration_dir)

        assert _states(result)["camera_calibration"] == (
            "todo",
            "校正ファイル old.json が無いか、焦点 Z が記録されていません",
        )
        # 装置診断の前提が残るので、前提待ちになる
        assert _states(result)["machine_check"] == (
            "blocked",
            "先に「カメラ校正」を済ませてください",
        )

    def test_unreadable_machine_toml(self, all_ok: Path, calibration_dir: Path):
        """設定エラーの間も、装置診断は直近の結果で判定し、ほかは判定できないと示す."""
        result = readiness(None, all_ok, calibration_dir)

        states = _states(result)
        assert states["camera_calibration"] == (
            "todo",
            "machine.toml を読めないため判定できません",
        )
        assert states["machine_check"] == ("done", STEPS[1].done_when)
        assert states["toolhead_offset"] == (
            "blocked",
            "先に「基準点」を済ませてください",
        )
        assert result.label == "立ち上げ 1/7"
