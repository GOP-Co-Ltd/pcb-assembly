"""Klipper の生成口 `hal.open_klipper` の契約テスト.

Klipper を作るのは `hal.open_klipper` だけで、それを呼ぶのは `pcbasm.machine.devices` だけである。
テストは `hal.open_klipper` を差し替えて実機への接続を断つので、それを迂回する生成箇所があってはならない。
装置を組み立てる場所を 1 か所にし、fake への切替もそこで行う。
"""

import ast
from pathlib import Path

from pcbasm.paths import PROJECT_ROOT

SRC_DIR = PROJECT_ROOT / "src"


def klipper_constructions(path: Path) -> list[str]:
    """ファイル内で `MoonrakerKlipper(...)` を呼ぶ関数名の一覧（モジュール直下は ``<module>``）."""
    found: list[str] = []

    def visit(node: ast.AST, scope: str) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            scope = node.name
        if (
            isinstance(node, ast.Call)
            and ast.unparse(node.func).split(".")[-1] == "MoonrakerKlipper"
        ):
            found.append(scope)
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(ast.parse(path.read_text(encoding="utf-8")), "<module>")
    return found


def calls_open_klipper(path: Path) -> bool:
    """ファイルに `open_klipper(...)` の呼び出しがあるか."""
    return any(
        isinstance(node, ast.Call)
        and ast.unparse(node.func).split(".")[-1] == "open_klipper"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
    )


def imports_open_klipper(path: Path) -> bool:
    """ファイルに `open_klipper` の from-import があるか."""
    return any(
        isinstance(node, ast.ImportFrom)
        and any(alias.name == "open_klipper" for alias in node.names)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
    )


class TestKlipperFactory:
    def test_only_open_klipper_constructs_klipper(self):
        constructions = [
            (str(path.relative_to(PROJECT_ROOT)), scope)
            for path in sorted(SRC_DIR.rglob("*.py"))
            for scope in klipper_constructions(path)
        ]

        assert constructions == [("src/pcbasm/hal/klipper.py", "open_klipper")]

    def test_only_devices_calls_open_klipper(self):
        callers = [
            str(path.relative_to(PROJECT_ROOT))
            for path in sorted(SRC_DIR.rglob("*.py"))
            if calls_open_klipper(path)
        ]

        assert callers == ["src/pcbasm/machine/devices.py"]

    def test_open_klipper_is_called_through_hal_module(self):
        # from-import は import 時点で実物に束縛され、テストの差し替えを迂回する
        importers = [
            str(path.relative_to(PROJECT_ROOT))
            for path in sorted(SRC_DIR.rglob("*.py"))
            if imports_open_klipper(path)
        ]

        assert importers == ["src/pcbasm/hal/__init__.py"]
