"""PnP の XYZ ファーム（src/firmware/pnp-motion）の、ホストで回せるテスト.

core/ の C のテストを `make test` で流し、CI に載せる（.uf2 のビルドは対象外）。
"""

import subprocess
from pathlib import Path

from pcbasm.paths import PROJECT_ROOT

FIRMWARE = PROJECT_ROOT / "src" / "firmware" / "pnp-motion"


class TestPnpMotionCore:
    def test_c_unit_tests_pass(self, tmp_path: Path):
        result = subprocess.run(
            ["make", "-s", "-C", str(FIRMWARE), "test", f"OUT={tmp_path}/"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.strip().endswith("ok")
