"""`pcbasm.machine.journal` の仕様テスト."""

import logging
import os
from datetime import date
from pathlib import Path

import pytest

from pcbasm.gcode import GCode
from pcbasm.machine.journal import LIMIT, Journal


class TestJournal:
    def test_appends_each_block_under_its_effective_timeout(self, tmp_path: Path):
        journal = Journal(tmp_path / "journal" / "job.gcode")

        journal.write(GCode("G28\nM400"), 60.0)
        journal.write(GCode("M84"), None)

        assert journal.path.read_text(encoding="utf-8") == (
            "# send timeout=60.0\nG28\nM400\n# send timeout=None\nM84\n"
        )

    def test_names_files_by_date_and_job(self, tmp_path: Path):
        today = date.today().isoformat()

        assert Journal.for_job(tmp_path, "abc").path == tmp_path / f"{today}-abc.gcode"
        assert Journal.for_today(tmp_path).path == tmp_path / f"{today}-rest.gcode"

    def test_new_file_removes_the_least_recently_written_beyond_limit(
        self, tmp_path: Path
    ):
        for index in range(LIMIT):
            path = tmp_path / f"{index:03}.gcode"
            path.write_text("M400\n", encoding="utf-8")
            os.utime(path, (index, index))

        Journal(tmp_path / "new.gcode").write(GCode("M400"), None)

        names = {path.name for path in tmp_path.glob("*.gcode")}
        assert len(names) == LIMIT
        assert "000.gcode" not in names
        assert {"001.gcode", "new.gcode"} <= names

    def test_appending_to_an_existing_file_removes_nothing(self, tmp_path: Path):
        for index in range(LIMIT):
            (tmp_path / f"{index:03}.gcode").write_text("M400\n", encoding="utf-8")

        Journal(tmp_path / "000.gcode").write(GCode("M400"), None)

        assert len(list(tmp_path.glob("*.gcode"))) == LIMIT

    def test_write_failure_is_logged_instead_of_raised(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        # ジャーナルが書けなくても送信を止めない
        (tmp_path / "journal").write_text("", encoding="utf-8")
        journal = Journal(tmp_path / "journal" / "job.gcode")

        with caplog.at_level(logging.WARNING):
            journal.write(GCode("M400"), None)

        assert "G-code ジャーナルに書けません" in caplog.text
