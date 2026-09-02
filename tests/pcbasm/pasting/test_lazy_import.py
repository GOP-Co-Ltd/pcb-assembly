from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[3]


class TestPastingLazyImport:
    def test_paste_volume_import_does_not_load_hardware_or_ml_dependencies(self):
        code = """
import json
import sys
import pcbasm.pasting.paste_volume

forbidden = ("pcbasm.hal", "picamera2", "cv2", "torch", "torchvision")
print(json.dumps([
    prefix
    for prefix in forbidden
    if any(name == prefix or name.startswith(f"{prefix}.") for name in sys.modules)
]))
"""

        completed = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )

        assert json.loads(completed.stdout) == []

    def test_public_exports_resolve_on_first_access(self):
        from pcbasm.config import DispenseMode as ConfigDispenseMode
        from pcbasm.pasting import DispenseMode, DispenseRateCalibration
        from pcbasm.pasting.dispense_calibration import (
            DispenseRateCalibration as ModuleDispenseRateCalibration,
        )

        assert DispenseMode is ConfigDispenseMode
        assert DispenseRateCalibration is ModuleDispenseRateCalibration
