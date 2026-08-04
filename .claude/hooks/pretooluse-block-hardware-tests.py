#!/usr/bin/env python3
"""実機テストを走らせうる Bash コマンドを PreToolUse で拒否するフック.

`make test` / `make run` と、`-m "not hardware"` を伴わない pytest 起動を止める。
実機（カメラ、Klipper 接続のステージ・サーボ・エアポンプ、GPIO）が物理的に動作し 破損や事故につながるため、Claude
とサブエージェントは実機テストを実行しない （`memory/MEMORY.md` の "No hardware test
execution"）。

指示文だけでは、サブエージェントが `uv run pytest tests/webui` のように 「実機テストを含むディレクトリを
marker 無しで指定する」経路を塞げないため、 機構として拒否する。

判定は「コマンド位置に pytest があるか」で行い、`grep pytest Makefile` のような
読み取り専用の言及は誤検出しない。
"""

from __future__ import annotations

import json
import re
import shlex
import sys

# コマンド位置を探す際に読み飛ばすラッパー（uv run / timeout 900 / env VAR=1 等）
_WRAPPER_TOKENS = frozenset(
    {
        "uv",
        "run",
        "exec",
        "env",
        "time",
        "timeout",
        "nohup",
        "stdbuf",
        "xargs",
        "python",
        "python3",
        "-m",
        "--no-sync",
        "--frozen",
        "--locked",
        "-u",
    }
)

# ループ / 条件分岐 / サブシェルの構文語（この後ろに実コマンドが続く）
_SHELL_KEYWORDS = frozenset(
    {
        "do",
        "then",
        "else",
        "elif",
        "{",
        "}",
        "(",
        ")",
        "!",
        "for",
        "while",
        "until",
        "if",
    }
)

# pytest を「引数として文字列で言及するだけ」のツール（grep pytest Makefile 等）
_TEXT_TOOLS = frozenset(
    {
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "sed",
        "awk",
        "cat",
        "head",
        "tail",
        "less",
        "more",
        "echo",
        "printf",
        "wc",
        "sort",
        "uniq",
        "cut",
        "tr",
        "find",
        "ls",
        "git",
        "jq",
        "diff",
        "comm",
    }
)

_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\||\n")
# ヒアドキュメント開始（<<EOF / <<'EOF' / <<-"EOF"）。herestring <<< は対象外
_HEREDOC_START = re.compile(
    r"""<<-?[ \t]*(?P<tag>'[^']+'|"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)"""
)
_NOT_HARDWARE = re.compile(r"not\s+hardware")
_SELECTS_HARDWARE = re.compile(r"""-m\s*["']?\s*hardware""")
# make test / make run（make test-no-hardware / test-e2e は対象外）
_MAKE_HARDWARE_TARGET = re.compile(r"\bmake\s+(?:-\w+\s+)*(test|run)(?![-\w])")

_FIX = (
    "実機テストは実行できません。検証には次を使ってください:\n"
    "  make test-no-hardware                        # 全体（実機除外）\n"
    '  uv run pytest <path> -m "not hardware" -q    # 範囲を絞る場合\n'
    "pytest を直接叩くときは対象パスに関係なく "
    '-m "not hardware" を必ず付けてください'
    "（tests/webui/ と tests/pcbasm/hal/ には @mark_hardware が含まれます）。"
)


def _strip_heredocs(command: str) -> str:
    """ヒアドキュメント本文を解析対象から除く.

    ``git commit -m "$(cat <<'EOF' … EOF)"`` の本文に "make test" のような
    文字列が現れても実行ではないため、誤検出を避ける。
    """
    lines = command.split("\n")
    result: list[str] = []
    terminator: str | None = None
    for line in lines:
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        result.append(line)
        match = _HEREDOC_START.search(line)
        if match is not None:
            terminator = match.group("tag").strip("\"'")
    return "\n".join(result)


def _segment_command(tokens: list[str]) -> str | None:
    """トークン列の先頭からコマンド名（basename）を特定する."""
    for token in tokens:
        if (
            "=" in token
            and not token.startswith("-")
            and "/" not in token.split("=")[0]
        ):
            continue  # VAR=value の環境変数前置
        if token in _WRAPPER_TOKENS or token in _SHELL_KEYWORDS or token.isdigit():
            continue
        return token.rsplit("/", 1)[-1]
    return None


def _is_pytest_token(token: str) -> bool:
    return token.rsplit("/", 1)[-1].startswith("pytest")


def _is_pytest_invocation(segment: str, _depth: int = 0) -> bool:
    """セグメントが pytest を起動するかを判定する.

    コマンド名を特定し、読み取り専用のテキストツール（grep 等）でなければ
    全トークンから pytest を探す。ループ本体・サブシェル・``bash -c "…"``
    のような入れ子も拾う。
    """
    try:
        tokens = shlex.split(segment, comments=True)
    except ValueError:
        # クォート不一致は判定不能。文字列として pytest を含むなら安全側に倒す
        return "pytest" in segment
    if _segment_command(tokens) in _TEXT_TOOLS:
        return False
    if any(_is_pytest_token(token) for token in tokens):
        return True
    # bash -c "…" のように 1 トークンへ埋め込まれたコマンド列を 1 段だけ展開する
    if _depth == 0:
        return any(
            " " in token and _is_pytest_invocation(token, _depth + 1)
            for token in tokens
        )
    return False


def _reason(command: str) -> str | None:
    """拒否理由を返す（許可なら None）."""
    for segment in _SEGMENT_SPLIT.split(_strip_heredocs(command)):
        try:
            tokens = shlex.split(segment, comments=True)
        except ValueError:
            tokens = segment.split()
        if _segment_command(tokens) == "make":
            target = _MAKE_HARDWARE_TARGET.search(segment)
            if target is not None:
                return f"`make {target.group(1)}` は実機テストを含みます。\n{_FIX}"
            continue
        if not _is_pytest_invocation(segment):
            continue
        if _SELECTS_HARDWARE.search(segment):
            return f"`-m hardware` は実機を動かします。\n{_FIX}"
        if not _NOT_HARDWARE.search(segment):
            return (
                '`-m "not hardware"` の無い pytest 起動です'
                f"（該当箇所: {segment.strip()[:120]}）。\n{_FIX}"
            )
    return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return  # 入力が読めないときは通す（fail-open。他フックと同じ方針）
    if payload.get("tool_name") != "Bash":
        return
    command = payload.get("tool_input", {}).get("command", "")
    if not isinstance(command, str):
        return
    reason = _reason(command)
    if reason is None:
        return
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
