"""`pcbasm.board.plot` の仕様テスト（実 PCB を描いて PNG として読めることだけを確かめる）."""

import cv2

from pcbasm.board.plot import render_pcb
from pcbasm.kicad import load_board
from tests.support.paths import TESTING_DATA_DIR

FILL_COVERAGE_PCB = TESTING_DATA_DIR / "fill_coverage" / "fill_coverage.kicad_pcb"


class TestRenderPcb:
    """render_pcb."""

    def test_renders_real_pcb_to_readable_png(self, tmp_path):
        pcb = load_board(FILL_COVERAGE_PCB)
        output = tmp_path / "pcb.png"

        render_pcb(pcb.outline, pcb.pads, pcb.copper, pcb.components, output)

        image = cv2.imread(str(output))
        assert image is not None
        assert image.size > 0
