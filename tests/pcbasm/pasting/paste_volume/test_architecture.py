"""paste_volume が学習機で動き続けることの機械検証.

このツリーは GPU 学習機・学習コンテナで検証する（``make ml-docker-check``）。そこには
``pcbnew`` も ``picamera2`` も無いので、装置 HAL へ届く import が 1 本でも入ると collect
できなくなる。規約ではなくテストで固定する。

``tests.helpers`` を禁じるのは、それが module 冒頭で ``pcbnew`` と ``picamera2`` を
import するため。``tests/ml/test_architecture.py`` と同じ理由・同じ手口。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.pcbasm.pasting.paste_volume.helpers import PROJECT_ROOT

SOURCE_ROOT = PROJECT_ROOT / "src" / "pcbasm" / "pasting" / "paste_volume"
TEST_ROOT = PROJECT_ROOT / "tests" / "pcbasm" / "pasting" / "paste_volume"

# 学習機に存在しない依存へ到達する module。
FORBIDDEN_IMPORTS = ("pcbasm.hal", "pcbnew", "picamera2")

# テスト側だけの追加禁止。tests.helpers は冒頭で pcbnew / picamera2 を import する。
FORBIDDEN_TEST_IMPORTS = (*FORBIDDEN_IMPORTS, "tests.helpers")


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def _reaches(module: str, forbidden: str) -> bool:
    return module == forbidden or module.startswith(f"{forbidden}.")


def _python_files(root: Path) -> list[Path]:
    return sorted(
        path for path in root.rglob("*.py") if "__pycache__" not in path.parts
    )


class TestPasteVolumeImports:
    """ドメイン層と、そのテストが装置 HAL へ届かないこと."""

    def test_the_source_tree_has_modules_to_check(self):
        """走査対象が空でないこと.

        対象 directory を取り違えると、以降の検査が全て素通りする。
        """

        assert _python_files(SOURCE_ROOT)
        assert _python_files(TEST_ROOT)

    @pytest.mark.parametrize("forbidden", FORBIDDEN_IMPORTS)
    def test_no_source_module_reaches_the_device_hal(self, forbidden: str):
        offenders = {
            path.relative_to(PROJECT_ROOT).as_posix()
            for path in _python_files(SOURCE_ROOT)
            for module in _imported_modules(path)
            if _reaches(module, forbidden)
        }

        assert not offenders, f"{forbidden} へ到達する module: {sorted(offenders)}"

    @pytest.mark.parametrize("forbidden", FORBIDDEN_TEST_IMPORTS)
    def test_no_test_module_reaches_the_device_hal(self, forbidden: str):
        offenders = {
            path.relative_to(PROJECT_ROOT).as_posix()
            for path in _python_files(TEST_ROOT)
            for module in _imported_modules(path)
            if _reaches(module, forbidden)
        }

        assert not offenders, f"{forbidden} へ到達する module: {sorted(offenders)}"

    def test_the_scanner_sees_an_import_it_should_reject(self):
        """検査器そのものが機能していることを確かめる.

        禁止 module を 1 つも含まないツリーでは、上の 2 つは常に緑になる。
        走査と判定が実際に働いているかは別途見る必要がある。
        """

        assert _reaches("pcbasm.hal", "pcbasm.hal")
        assert _reaches("pcbasm.hal.camera", "pcbasm.hal")
        assert not _reaches("pcbasm.halt", "pcbasm.hal")
