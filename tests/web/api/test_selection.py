"""`web.api.selection.BoardCache` の仕様テスト（実 pcbnew で読む）."""

import shutil
from pathlib import Path

from tests.web.api.conftest import COPPER_PCB_FIXTURE, REAL_PCB_FIXTURE
from web.api.selection import BoardCache


class TestBoardCache:
    def test_unchanged_file_is_not_read_again(self, tmp_path: Path):
        path = tmp_path / "board.kicad_pcb"
        shutil.copy(COPPER_PCB_FIXTURE, path)
        cache = BoardCache()

        assert cache.get(path) is cache.get(path)

    def test_file_replaced_under_the_same_name_is_read_again(self, tmp_path: Path):
        path = tmp_path / "board.kicad_pcb"
        shutil.copy(COPPER_PCB_FIXTURE, path)
        cache = BoardCache()
        before = cache.get(path)

        shutil.copy(REAL_PCB_FIXTURE, path)
        after = cache.get(path)

        assert {pad.designator for pad in before.pads} >= {"U1", "R1"}
        assert "CONCAVE_L" in {pad.designator for pad in after.pads}

    def test_forget_reads_again_even_if_the_file_is_unchanged(self, tmp_path: Path):
        path = tmp_path / "board.kicad_pcb"
        shutil.copy(COPPER_PCB_FIXTURE, path)
        cache = BoardCache()
        before = cache.get(path)

        cache.forget()

        assert cache.get(path) is not before
        assert cache.get(path) == before
