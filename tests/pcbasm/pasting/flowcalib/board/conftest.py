"""流量キャリブレーション基板core testのfixture."""

from pathlib import Path

import pytest

from pcbasm.pasting.flowcalib.board.generator import (
    BoardGenerator,
)


@pytest.fixture
def generator(
    paste_flow_calibration_footprint_root: Path,
) -> BoardGenerator:
    return BoardGenerator(paste_flow_calibration_footprint_root)
