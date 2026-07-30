"""`.claude/hooks/` の安全フックの契約テスト.

`pretooluse-block-hardware-tests.py` は実機（ステージ・サーボ・エアポンプ・
ディスペンサー・カメラ）を物理的に動かしうるコマンドを止める最後の砦なので、 判定ロジックの回帰をここでピンする。フックの公開契約は「stdin
に PreToolUse の JSON を受け、拒否するときだけ stdout に permissionDecision=deny
を書く」であり、 内部関数ではなくその契約を実プロセス起動で検証する。

`tests/test_package.py` と同じくミラーレイアウト外のトップレベルテスト （`src/` に対応物が無い成果物のため）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pcbasm.utils import PROJECT_ROOT

HOOK = PROJECT_ROOT / ".claude" / "hooks" / "pretooluse-block-hardware-tests.py"

# git commit のヒアドキュメント本文に "make test" が現れるが実行ではないケース
_COMMIT_WITH_HEREDOC = """git commit -q -m "$(cat <<'EOF'
chore(safety): 実機テスト実行を禁止する

`make test` の deny だけでは `uv run pytest tests/webui` が通る。
EOF
)\""""

BLOCKED = [
    pytest.param("make test", id="make-test"),
    pytest.param("make run", id="make-run-includes-test"),
    pytest.param("for i in 1 2; do make test; done", id="make-test-in-loop"),
    pytest.param("uv run pytest tests/webui -q", id="pytest-without-marker"),
    pytest.param(
        "timeout 900 uv run pytest tests/webui -q -x --timeout=120 2>&1 | tail -5",
        id="pytest-wrapped-in-timeout",
    ),
    pytest.param(".venv/bin/pytest tests/webui -q", id="pytest-absolute-path"),
    pytest.param("uv run pytest -m hardware", id="explicitly-selects-hardware"),
    pytest.param(
        'uv run pytest -m "hardware"', id="explicitly-selects-hardware-quoted"
    ),
    pytest.param(
        "for i in 1 2 3; do uv run pytest tests/webui/test_state.py -q; done",
        id="pytest-in-loop-body",
    ),
    pytest.param(
        'bash -c "uv run pytest tests/webui -q"', id="pytest-nested-in-bash-c"
    ),
    pytest.param(
        "cd /home/gop/pcb-assembly && uv run pytest tests/pcbasm/hal -q",
        id="pytest-after-cd",
    ),
    pytest.param("cat <<EOF\nfoo\nEOF\nmake test", id="make-test-after-heredoc"),
]

ALLOWED = [
    pytest.param("make test-no-hardware", id="make-test-no-hardware"),
    pytest.param("make test-e2e", id="make-test-e2e"),
    pytest.param(
        "make format && make type && make test-no-hardware", id="verification-chain"
    ),
    pytest.param(
        'uv run pytest -v -m "not hardware and not e2e"', id="marker-excluded"
    ),
    pytest.param(
        'uv run pytest tests/webui/test_atomic.py -q -m "not hardware"',
        id="scoped-with-marker",
    ),
    pytest.param(
        'PYTHONPATH=/tmp uv run pytest tests/x -m "not hardware" -q',
        id="env-prefix-with-marker",
    ),
    pytest.param(
        'for i in 1 2 3; do uv run pytest tests/x -m "not hardware" -q; done',
        id="loop-with-marker",
    ),
    pytest.param("grep -rn pytest Makefile", id="grep-mentions-pytest"),
    pytest.param("uv run pyright", id="type-check"),
    pytest.param(_COMMIT_WITH_HEREDOC, id="commit-message-mentions-make-test"),
    pytest.param(
        'git commit -m "docs: describe make test behaviour"',
        id="commit-subject-mentions-make-test",
    ),
    pytest.param("cat <<EOF\nmake test\nEOF", id="heredoc-body-mentions-make-test"),
]


def _decide(command: str, *, tool: str = "Bash") -> str | None:
    """フックを実プロセスで起動し、拒否理由を返す（許可なら None）."""
    completed = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_name": tool, "tool_input": {"command": command}}),
        capture_output=True,
        text=True,
        check=True,
    )
    if not completed.stdout.strip():
        return None
    payload = json.loads(completed.stdout)["hookSpecificOutput"]
    assert payload["hookEventName"] == "PreToolUse"
    assert payload["permissionDecision"] == "deny"
    return payload["permissionDecisionReason"]


class TestBlockHardwareTests:
    """実機テストを走らせうる Bash コマンドの拒否判定."""

    def test_hook_exists_and_is_wired(self):
        assert HOOK.is_file()
        settings = json.loads((PROJECT_ROOT / ".claude" / "settings.json").read_text())
        commands = [
            hook["command"]
            for entry in settings["hooks"]["PreToolUse"]
            for hook in entry["hooks"]
        ]
        assert any(HOOK.name in command for command in commands)

    @pytest.mark.parametrize("command", BLOCKED)
    def test_blocks(self, command: str):
        reason = _decide(command)
        assert reason is not None, f"拒否されなかった: {command}"
        assert "make test-no-hardware" in reason  # 代替手段を必ず案内する

    @pytest.mark.parametrize("command", ALLOWED)
    def test_allows(self, command: str):
        assert _decide(command) is None, f"誤って拒否された: {command}"

    def test_ignores_non_bash_tools(self):
        assert _decide("uv run pytest tests/webui -q", tool="Read") is None

    def test_passes_through_unreadable_input(self):
        """入力が JSON でないときは通す（fail-open。他フックと同じ方針）."""
        completed = subprocess.run(
            [sys.executable, str(HOOK)],
            input="not json",
            capture_output=True,
            text=True,
            check=True,
        )
        assert completed.stdout.strip() == ""


class TestHardwareMarkerLayout:
    """フックの前提（marker 無し pytest が実機到達しうる）を検証する.

    実機テストを含むディレクトリが実在することを確認する。もし将来 `@mark_hardware` が `tests/`
    から一掃されたなら、この前提もフックの厳しさも見直してよい。
    """

    def test_hardware_marked_tests_exist_outside_hal(self):
        marked = {
            path.relative_to(PROJECT_ROOT).as_posix()
            for path in (PROJECT_ROOT / "tests").rglob("test_*.py")
            if "@mark_hardware" in path.read_text(encoding="utf-8")
        }
        assert marked, "@mark_hardware を含むテストが 1 つも無い"
        # webui 配下にも実機テストがある = `pytest tests/webui` は marker 必須
        assert any(name.startswith("tests/webui/") for name in marked)
