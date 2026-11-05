"""普段は短いテストだけを流し、CI と同じフルテストは別ターゲットに残す契約テスト.

`make check` は開発中に何度も流すので、遅いテスト（slow）と E2E を除いて並列で走らせる。

CI が流す `make test` は slow を除かない。
"""

import subprocess

import pytest

from pcbasm.paths import PROJECT_ROOT


def dry_run(target: str) -> str:
    """`make -n <target>` が表示するコマンド列を返す."""
    result = subprocess.run(
        ["make", "-n", target],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return result.stdout


def pytest_command(target: str) -> str:
    """ターゲットが実行する pytest のコマンド行を返す."""
    commands = dry_run(target)
    lines = [line for line in commands.splitlines() if "pytest" in line]
    assert len(lines) == 1, commands
    return lines[0]


class TestShortTarget:
    def test_check_runs_the_short_tests(self):
        assert pytest_command("check") == pytest_command("test-short")

    @pytest.mark.parametrize("option", ('-m "not e2e and not slow"', "-n auto"))
    def test_short_skips_slow_and_e2e_in_parallel(self, option: str):
        assert option in pytest_command("test-short")


class TestFullTarget:
    def test_full_tests_keep_the_slow_ones(self):
        assert "slow" not in pytest_command("test")
