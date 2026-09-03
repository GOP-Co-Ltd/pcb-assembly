from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pcbasm
from tests.helpers import PROJECT_ROOT


def test_version() -> None:
    with open(PROJECT_ROOT / "pyproject.toml", "rb") as f:
        pyproject = tomllib.load(f)

    assert pcbasm.__version__ == pyproject["project"]["version"]


class TestPastingImportLight:
    """``import pcbasm.pasting`` が重い依存を eager import しない契約."""

    def test_heavy_modules_are_not_imported(self):
        # 他テストが cv2 等を既に読み込んでいるため、素の interpreter で確認する
        code = (
            "import sys; import pcbasm.pasting; "
            "print(sorted({'cv2', 'pcbnew', 'torch'} & set(sys.modules)))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            cwd=PROJECT_ROOT,
        )
        assert result.stdout.strip() == "[]"
