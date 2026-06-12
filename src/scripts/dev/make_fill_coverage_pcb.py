#!/usr/bin/env python3
"""Fill ロジック要件網羅用の KiCad PCB フィクスチャを生成するスクリプト.

基板の構築は ``pcbasm.pcb.generate.build_fill_coverage_board``（面 / 凹形 /
線 / 点 の各塗布分岐を網羅する F.Paste パッド群）。本スクリプトは
リポジトリ内フィクスチャへの保存のみを行う。出力先::

    data/testing/fill_coverage/fill_coverage.kicad_pcb

実行::

    uv run python -m scripts.dev.make_fill_coverage_pcb
"""

from __future__ import annotations

from pcbasm.pcb.generate import build_fill_coverage_board, save_board
from pcbasm.utils import PROJECT_ROOT

_OUTPUT = (
    PROJECT_ROOT / "data" / "testing" / "fill_coverage" / "fill_coverage.kicad_pcb"
)


def main() -> None:
    """フィクスチャ PCB を生成して保存する."""
    save_board(build_fill_coverage_board(), _OUTPUT)
    print(f"保存しました: {_OUTPUT}")


if __name__ == "__main__":
    main()
