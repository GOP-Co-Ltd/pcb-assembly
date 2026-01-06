from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent

TESTING_DATA_DIR = PROJECT_ROOT / "data" / "testing"

mark_hardware = pytest.mark.hardware
