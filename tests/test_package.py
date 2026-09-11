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


HEAVY_MODULES = "{'cv2', 'pcbnew', 'picamera2'}"


def _imported_heavy_modules(target: str) -> str:
    """素の interpreter で target を import し、同時に載った重い依存を返す.

    他テストが cv2 等を既に読み込んでいるため、必ず子プロセスで確認する。
    """
    code = (
        f"import sys; import {target}; "
        f"print(sorted({HEAVY_MODULES} & set(sys.modules)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=PROJECT_ROOT,
    )
    return result.stdout.strip()


class TestPastingImportLight:
    """``import pcbasm.pasting`` が重い依存を eager import しない契約."""

    def test_heavy_modules_are_not_imported(self):
        assert _imported_heavy_modules("pcbasm.pasting") == "[]"


class TestSelfUpdateImportLight:
    """``import web.selfupdate`` が装置ドメインの依存を引かない契約.

    UI frontend 専用機には pcbnew（KiCAD）も picamera2 も入っていない。
    更新モジュールがこれらを引くと、自分自身を更新できない機体が生まれる。
    """

    def test_heavy_modules_are_not_imported(self):
        assert _imported_heavy_modules("web.selfupdate") == "[]"

    def test_the_runner_alone_is_also_light(self):
        """`web.selfupdate.runner` を直接 import する経路（router / update_api）も同じ."""
        assert _imported_heavy_modules("web.selfupdate.runner") == "[]"
