"""paste_volume が学習機で動き続けることの機械検証.

このツリーは GPU 学習機・学習コンテナで検証する（``make ml-docker-check``）。そこには
``pcbnew`` も ``picamera2`` も無いので、装置 HAL へ届く import が 1 本でも入ると collect
できなくなる。規約ではなくテストで固定する。

**直接の import だけでなく推移的な到達も見る。** この MR のブロッカーは
``dataset.metadata`` から ``applicator`` を経て ``pcbasm.hal`` へ届く 2 段の連鎖で、
直接 import しか見ない検査では捕まらなかった。CI は picamera2 のある Raspberry Pi で
走るので、実行時に落ちることにも頼れない。

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


def _module_file(module: str) -> Path | None:
    """自前 module の実体 file を返す。見つからなければ ``None``."""

    relative = Path(*module.split("."))
    for candidate in (
        PROJECT_ROOT / "src" / relative.with_suffix(".py"),
        PROJECT_ROOT / "src" / relative / "__init__.py",
    ):
        if candidate.is_file():
            return candidate
    return None


def _reachable_modules(entries: list[Path]) -> set[str]:
    """起点から自前 module の import をたどって到達する module 名を集める.

    package を import すると ``__init__`` が走るので、``a.b.c`` を見たら ``a`` と ``a.b``
    も到達したものとして数える。この MR のブロッカーは ``dataset.metadata`` から
    ``vision.image`` を読んだ結果 ``pcbasm.vision`` の ``__init__`` が走る形だった。
    """

    seen: set[str] = set()
    pending = list(entries)
    while pending:
        path = pending.pop()
        for module in _imported_modules(path):
            parts = module.split(".")
            for depth in range(1, len(parts) + 1):
                ancestor = ".".join(parts[:depth])
                if ancestor in seen:
                    continue
                seen.add(ancestor)
                if (target := _module_file(ancestor)) is not None:
                    pending.append(target)
    return seen


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

    @pytest.mark.parametrize("forbidden", FORBIDDEN_IMPORTS)
    def test_nothing_reaches_the_device_hal_through_a_chain(self, forbidden: str):
        """推移的にも装置 HAL へ届かないこと.

        収集 schema も対象へ入れる。ドメイン層はそこを必ず読むので、schema 側が HAL を 引き戻した瞬間に学習機で
        collect できなくなる。
        """

        entries = [
            *_python_files(SOURCE_ROOT),
            *_python_files(TEST_ROOT),
            PROJECT_ROOT / "src" / "pcbasm" / "pasting" / "dataset" / "metadata.py",
        ]

        offenders = {
            module
            for module in _reachable_modules(entries)
            if _reaches(module, forbidden)
        }

        assert not offenders, f"{forbidden} へ到達します: {sorted(offenders)}"

    def test_the_transitive_scan_follows_more_than_one_hop(self):
        """連鎖をたどる検査そのものが働いていることを確かめる.

        上の検査は「到達しないこと」を見るので、走査を弱めるほど通りやすくなる。
        再帰を落としても緑のままなら、検査は直接 import しか見ない版へ黙って退化する。

        起点は ``dataset/recorder.py``。``applicator`` を経て ``pcbasm.hal`` へ届く
        **本 MR のブロッカーと同じ 2 段の形**で、直接は HAL を import していない。
        ``applicator`` 自身を起点にすると 1 段で届くので、再帰の検証にならない。
        """

        recorder = (
            PROJECT_ROOT / "src" / "pcbasm" / "pasting" / "dataset" / "recorder.py"
        )

        assert not any(
            _reaches(module, "pcbasm.hal") for module in _imported_modules(recorder)
        )
        assert any(
            _reaches(module, "pcbasm.hal") for module in _reachable_modules([recorder])
        )

    def test_the_scanner_sees_an_import_it_should_reject(self):
        """検査器そのものが機能していることを確かめる.

        禁止 module を 1 つも含まないツリーでは、上の 2 つは常に緑になる。
        走査と判定が実際に働いているかは別途見る必要がある。
        """

        assert _reaches("pcbasm.hal", "pcbasm.hal")
        assert _reaches("pcbasm.hal.camera", "pcbasm.hal")
        assert not _reaches("pcbasm.halt", "pcbasm.hal")
