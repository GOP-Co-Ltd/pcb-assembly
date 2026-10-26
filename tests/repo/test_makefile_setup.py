"""`make setup` は uv.lock を書き換えずに venv を作り直す契約テスト.

自己更新は作業ツリーが dirty だと止まるので、開発機で setup を流しても uv.lock を変えてはならない。
"""

import subprocess

import pytest

from pcbasm.paths import PROJECT_ROOT


def setup_command(program: str) -> list[str]:
    """`make -n setup` が表示するコマンドのうち、指定した uv サブコマンドの argv を返す."""
    result = subprocess.run(
        ["make", "-n", "setup"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    commands = [line.split() for line in result.stdout.splitlines()]
    matches = [argv for argv in commands if argv[:2] == ["uv", program]]
    assert len(matches) == 1, result.stdout
    return matches[0]


class TestSetupTarget:
    @pytest.mark.parametrize(
        ("program", "flag"),
        (
            ("sync", "--locked"),
            ("venv", "--system-site-packages"),
        ),
    )
    def test_command_has_flag(self, program: str, flag: str):
        assert flag in setup_command(program)
