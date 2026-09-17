from __future__ import annotations

import subprocess
import sys
import tomllib

import pytest

import pcbasm
from tests.helpers import PROJECT_ROOT


def _imported_heavy_modules(target: str) -> str:
    """素の interpreter で target を import し、同時に載った重い依存を返す.

    他テストが cv2 等を既に読み込んでいるため、必ず子プロセスで確認する。
    """
    code = (
        f"import sys; import {target}; "
        "print(sorted({'cv2', 'pcbnew', 'picamera2'} & set(sys.modules)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=PROJECT_ROOT,
    )
    return result.stdout.strip()


class TestPackageMetadata:
    def test_version_matches_project_metadata(self):
        with open(PROJECT_ROOT / "pyproject.toml", "rb") as f:
            pyproject = tomllib.load(f)

        assert pcbasm.__version__ == pyproject["project"]["version"]


class TestLightweightImports:
    """軽量な入口と frontend は、画像・実機用ライブラリを eager import しない.

    UI 専用ホストには pcbnew や picamera2 が無い。ページや自己更新の import に
    これらが混入すると、機体以外のホストでは起動できなくなる。
    """

    @pytest.mark.parametrize(
        "target",
        ("pcbasm.pasting", "web.selfupdate", "web.selfupdate.runner", "web.ui.app"),
    )
    def test_entry_points_do_not_import_machine_libraries(self, target: str):
        assert _imported_heavy_modules(target) == "[]"
