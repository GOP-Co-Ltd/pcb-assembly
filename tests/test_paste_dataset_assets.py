"""ペースト塗布データセット収集で使う repository asset の契約テスト."""

from pathlib import Path

import pytest

from pcbasm.pcb import PcbFile
from tests.helpers import PROJECT_ROOT


class TestPasteTestBoardAssets:
    """生成済み calibration board を任意PCBとして読み込めることを検証する."""

    @pytest.mark.parametrize(
        ("filename", "expected_pad_count"),
        [
            ("paste-test-board.basic.kicad_pcb", 181),
            ("paste-test-board.cricles_and_rectangles.kicad_pcb", 91),
        ],
    )
    def test_keeps_pad_count_and_canonical_purge_pad(
        self, filename: str, expected_pad_count: int
    ):
        path = PROJECT_ROOT / "data" / "paste-test-board" / filename

        pcb = PcbFile(path)

        assert len(pcb.pads) == expected_pad_count
        purge = [pad for pad in pcb.pads if pad.designator == "PURGE"]
        assert len(purge) == 1
        assert purge[0].pad_number == "1"


class TestPasteDatasetStorageAsset:
    """永続dataset rootは空ディレクトリのままGit管理できる."""

    def test_generated_sessions_are_ignored_but_gitignore_is_not(self):
        root = PROJECT_ROOT / "data" / "paste-volume-datasets"
        ignore = root / ".gitignore"

        assert root.is_dir()
        assert ignore.read_text(encoding="utf-8").splitlines() == [
            "*",
            "!.gitignore",
        ]


class TestPasteVolumeCalibrationAsset:
    """校正ファイルの保存先は Git 管理する（dataset root と扱いが違う）."""

    def test_the_calibration_directory_is_tracked_by_git(self):
        """.gitignore を置かないことが「Git 管理する」の実体."""
        root = PROJECT_ROOT / "data" / "paste-volume-calibrations"

        assert root.is_dir()
        assert not (root / ".gitignore").exists()

    def test_the_calibration_directory_explains_itself(self):
        readme = PROJECT_ROOT / "data" / "paste-volume-calibrations" / "README.md"

        assert "Git 管理する" in readme.read_text(encoding="utf-8")
