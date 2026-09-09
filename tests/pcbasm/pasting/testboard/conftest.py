"""テスト塗布基板core testのfixture."""

from pathlib import Path

import pytest

from pcbasm.pasting.testboard.generator import (
    BoardGenerator,
)


@pytest.fixture
def generator(
    paste_test_board_footprint_root: Path,
) -> BoardGenerator:
    return BoardGenerator(paste_test_board_footprint_root)
