"""ゲームパッドの生成口 `hal.open_gamepad` の契約テスト.

`EvdevGamepad` と evdev の `InputDevice` を作るのは `hal.open_gamepad`（`pcbasm.hal.gamepad`）だけである。テストは
`hal.open_gamepad` を差し替えて実機のコントローラを読ませないので、それを迂回する生成箇所があってはならない。
"""

import ast
from pathlib import Path

from pcbasm.paths import PROJECT_ROOT

SRC_DIR = PROJECT_ROOT / "src"


def constructions(path: Path, names: set[str]) -> list[str]:
    """ファイル内で names のどれかを呼ぶ関数名の一覧（モジュール直下は ``<module>``）."""
    found: list[str] = []

    def visit(node: ast.AST, scope: str) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            scope = node.name
        if (
            isinstance(node, ast.Call)
            and ast.unparse(node.func).split(".")[-1] in names
        ):
            found.append(scope)
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(ast.parse(path.read_text(encoding="utf-8")), "<module>")
    return found


def imports_open_gamepad(path: Path) -> bool:
    """ファイルに `open_gamepad` の from-import があるか."""
    return any(
        isinstance(node, ast.ImportFrom)
        and any(alias.name == "open_gamepad" for alias in node.names)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
    )


class TestGamepadFactory:
    def test_only_open_gamepad_constructs_the_gamepad_and_the_device(self):
        # 公開の生成口は open_gamepad 1 つ。_gamepad はその中だけで使う補助関数
        sites = sorted(
            (str(path.relative_to(PROJECT_ROOT)), scope)
            for path in sorted(SRC_DIR.rglob("*.py"))
            for scope in constructions(path, {"EvdevGamepad", "InputDevice"})
        )

        assert sites == [
            ("src/pcbasm/hal/gamepad.py", "_gamepad"),
            ("src/pcbasm/hal/gamepad.py", "open_gamepad"),
            ("src/pcbasm/hal/gamepad.py", "open_gamepad"),
        ]

    def test_open_gamepad_is_called_through_hal_module(self):
        # from-import は import 時点で実物に束縛され、テストの差し替えを迂回する
        importers = [
            str(path.relative_to(PROJECT_ROOT))
            for path in sorted(SRC_DIR.rglob("*.py"))
            if imports_open_gamepad(path)
        ]

        assert importers == ["src/pcbasm/hal/__init__.py"]
