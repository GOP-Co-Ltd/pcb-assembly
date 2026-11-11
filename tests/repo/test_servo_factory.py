"""サーボのバスの生成口 `hal.open_servo_bus` の契約テスト.

`FeetechBus` と scservo_sdk の `PortHandler` を作るのは `hal.open_servo_bus` だけで、それを呼ぶのは
`pcbasm.machine.devices` だけである。テストは `hal.open_servo_bus` を差し替えて実機のシリアルを
開かせないので、それを迂回する生成箇所があってはならない。
"""

import ast
from pathlib import Path

from pcbasm.paths import PROJECT_ROOT
from tests.support.paths import python_sources


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


def imports_open_servo_bus(path: Path) -> bool:
    """ファイルに `open_servo_bus` の from-import があるか."""
    return any(
        isinstance(node, ast.ImportFrom)
        and any(alias.name == "open_servo_bus" for alias in node.names)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
    )


def _sites(names: set[str]) -> list[tuple[str, str]]:
    return [
        (str(path.relative_to(PROJECT_ROOT)), scope)
        for path in python_sources()
        for scope in constructions(path, names)
    ]


class TestServoFactory:
    def test_only_open_servo_bus_constructs_the_bus_and_the_port(self):
        assert _sites({"FeetechBus", "PortHandler"}) == [
            ("src/pcbasm/hal/servo.py", "open_servo_bus"),
            ("src/pcbasm/hal/servo.py", "open_servo_bus"),
        ]

    def test_only_devices_calls_open_servo_bus(self):
        callers = sorted({path for path, _ in _sites({"open_servo_bus"})})

        assert callers == ["src/pcbasm/machine/devices.py"]

    def test_open_servo_bus_is_called_through_hal_module(self):
        # from-import は import 時点で実物に束縛され、テストの差し替えを迂回する
        importers = [
            str(path.relative_to(PROJECT_ROOT))
            for path in python_sources()
            if imports_open_servo_bus(path)
        ]

        assert importers == ["src/pcbasm/hal/__init__.py"]
