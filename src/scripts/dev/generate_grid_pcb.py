#!/usr/bin/env python3
"""直行性テスト用グリッドPCBを生成するスクリプト.

コアロジックは ``pcbasm.pcb.generate.generate_grid_pcb``。本スクリプトは
CLI 引数のパースと委譲のみを行う。
"""

import argparse
from pathlib import Path

from pcbasm.pcb.generate import generate_grid_pcb


def main() -> None:
    parser = argparse.ArgumentParser(description="直行性テスト用グリッドPCB生成")
    parser.add_argument(
        "-s",
        "--size",
        type=float,
        default=40,
        help="基板の一辺の長さ (mm, デフォルト: 30)",
    )
    parser.add_argument(
        "-n",
        "--divisions",
        type=int,
        default=3,
        help="グリッド分割数 (デフォルト: 2, パッド数=n^2)",
    )
    parser.add_argument(
        "--pad-size",
        type=float,
        default=0.5,
        help="パッドの一辺の長さ (mm, デフォルト: 1.0)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        help="出力 .kicad_pcb ファイルパス",
    )
    args = parser.parse_args()
    generate_grid_pcb(args.size, args.divisions, args.pad_size, args.output)


if __name__ == "__main__":
    main()
