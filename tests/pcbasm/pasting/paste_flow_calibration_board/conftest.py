"""流量キャリブレーション基板core testのfixture."""

from pathlib import Path

import pytest

from pcbasm.pasting.paste_flow_calibration_board.generator import (
    PasteFlowCalibrationBoardGenerator,
)


@pytest.fixture
def generator(
    paste_flow_calibration_footprint_root: Path,
) -> PasteFlowCalibrationBoardGenerator:
    return PasteFlowCalibrationBoardGenerator(paste_flow_calibration_footprint_root)
