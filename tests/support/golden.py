"""送信 G-code の golden（記録と比較）.

golden はジャーナルと同じ書式で、送信 1 回を ``# send timeout=<実効値>`` の見出しと G-code の行で書く。
比べるときは G-code を字句に分け、命令と語の並びは完全一致、数値は |Δ| ≤ 1e-9 で比べる。
float の末尾桁の揺れは吸収し、送る列の意味は変えさせない。見出しは完全一致で比べる。
``PCBASM_UPDATE_GOLDEN=1`` のときだけ、比べずに golden を書き換える。
"""

import os
import re
from collections.abc import Iterable
from pathlib import Path

import pytest

from pcbasm.hal.fake import SentGCode
from pcbasm.machine.journal import format_block

UPDATE_ENV = "PCBASM_UPDATE_GOLDEN"

TOLERANCE = 1e-9

# 語の末尾の数値（"X12.5" → "X" と "12.5"、"MOVE=-0.1" → "MOVE=" と "-0.1"）
_NUMBER = re.compile(r"(.*?)([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)")


def transcript(sent: Iterable[SentGCode]) -> str:
    """送信の列をジャーナルと同じ書式の文字列にする."""
    return "".join(format_block(block.gcode, block.timeout) for block in sent)


def assert_transcript(sent: Iterable[SentGCode], path: Path) -> None:
    """送信の列が golden ``path`` と同じであることを確かめる."""
    actual = transcript(sent)
    if os.environ.get(UPDATE_ENV) == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        return
    if not path.exists():
        pytest.fail(f"golden {path} がありません（{UPDATE_ENV}=1 で作る）")
    difference = _difference(path.read_text(encoding="utf-8"), actual)
    if difference is not None:
        pytest.fail(f"golden {path.name} と違います: {difference}")


def _difference(expected: str, actual: str) -> str | None:
    expected_lines = expected.splitlines()
    actual_lines = actual.splitlines()
    for number, (want, got) in enumerate(zip(expected_lines, actual_lines), start=1):
        if not _same_line(want, got):
            return f"{number} 行目 golden={want!r} 実際={got!r}"
    if len(expected_lines) != len(actual_lines):
        return f"行数 golden={len(expected_lines)} 実際={len(actual_lines)}"
    return None


def _same_line(expected: str, actual: str) -> bool:
    if expected.startswith("#") or actual.startswith("#"):
        return expected == actual
    want = expected.split()
    got = actual.split()
    return len(want) == len(got) and all(map(_same_word, want, got))


def _same_word(expected: str, actual: str) -> bool:
    want = _NUMBER.fullmatch(expected)
    got = _NUMBER.fullmatch(actual)
    if want is None or got is None:
        return expected == actual
    return want[1] == got[1] and abs(float(want[2]) - float(got[2])) <= TOLERANCE
