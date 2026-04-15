#!/usr/bin/env python3
"""直行性テスト用グリッドPCBを生成するスクリプト."""

import argparse
from pathlib import Path

import pcbnew


def generate_grid_pcb(
    size: float, divisions: int, pad_size: float, output: Path
) -> None:
    """正方形テストPCBにn^2個のグリッドパッドを配置して保存する."""
    board = pcbnew.BOARD()

    # 1. Board outline on Edge.Cuts (4 line segments forming a square)
    corners_mm = [(0, 0), (size, 0), (size, size), (0, size)]
    for k in range(4):
        x1, y1 = corners_mm[k]
        x2, y2 = corners_mm[(k + 1) % 4]
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x1), pcbnew.FromMM(y1)))
        seg.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(x2), pcbnew.FromMM(y2)))
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetWidth(pcbnew.FromMM(0.1))
        board.Add(seg)

    # 2. Create pads at grid intersections
    n = divisions
    step = size / (n + 1)
    pad_idx = 1
    for j in range(1, n + 1):  # row (Y)
        for i in range(1, n + 1):  # col (X)
            x_mm = i * step
            y_mm = j * step
            designator = f"P{pad_idx}"

            fp = pcbnew.FOOTPRINT(board)
            fp.SetReference(designator)
            fp.SetValue("GridPad")
            fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x_mm), pcbnew.FromMM(y_mm)))
            fp.SetLayer(pcbnew.F_Cu)

            pad = pcbnew.PAD(fp)
            pad.SetNumber("1")
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetShape(pcbnew.PAD_SHAPE_RECT)
            pad.SetSize(
                pcbnew.VECTOR2I(pcbnew.FromMM(pad_size), pcbnew.FromMM(pad_size))
            )
            pad.SetLayerSet(pad.SMDMask())  # F.Cu + F.Paste + F.Mask
            fp.Add(pad)

            board.Add(fp)
            pad_idx += 1

    # 3. Save
    output.parent.mkdir(parents=True, exist_ok=True)
    pcbnew.SaveBoard(str(output), board)

    print(f"Generated: {output}")
    print(f"Board: {size}x{size} mm")
    print(f"Grid: {n}x{n} = {n * n} pads (step={step:.1f} mm)")
    print(f"Pad size: {pad_size}x{pad_size} mm")


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
