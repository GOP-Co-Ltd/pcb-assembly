#!/usr/bin/env python3
"""CoreXY直行性テストスクリプト（カメラベース）.

生成したグリッドPCBを使い、カメラで各交点を対話的に巡回して
ベルトテンション・直行性を目視検証する。

処理フロー:
1. setup_board_calibrationでキャリブレーション
2. ボード四隅を対話的に巡回（テンション調整）
3. グリッド交点を対話的に巡回
4. 1に戻る（Ctrl+Cで終了）
"""

import argparse
import logging
from pathlib import Path

from pcbasm.config import get_machine_config
from pcbasm.geometry import Point2d, sort_by_nearest
from pcbasm.pcb import Layer
from pcbasm.posctrl import (
    interactive_display_at_point,
    machine_session,
    setup_board_calibration,
)
from pcbasm.utils import setup_logging

WINDOW_NAME = "Orthogonality Test"


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(
        description="CoreXY直行性テスト（カメラベース）",
    )
    parser.add_argument(
        "--machine",
        "-m",
        type=str,
        default="pd_china_frame",
        help="マシン名",
    )
    parser.add_argument(
        "--pcb-file",
        "-p",
        type=Path,
        required=True,
        help="KiCADファイル (.kicad_pcb) のパス",
    )
    parser.add_argument(
        "--tolerance",
        "-t",
        type=float,
        default=0.1,
        help="位置合わせの許容誤差 (mm)",
    )
    args = parser.parse_args()

    machine = get_machine_config(args.machine)
    result = setup_board_calibration(
        machine=machine,
        pcb_file_path=args.pcb_file,
        tolerance=args.tolerance,
        window_name=WINDOW_NAME,
    )

    with machine_session(result.klipper):
        board_transform = result.board_transform
        outline = result.pcb.outline

        # Define corners in board coordinates
        corners = [
            ("Top-Left", Point2d(0.0, 0.0)),
            ("Top-Right", Point2d(outline.width, 0.0)),
            ("Bottom-Right", Point2d(outline.width, outline.height)),
            ("Bottom-Left", Point2d(0.0, outline.height)),
        ]

        # Get pad centers for grid tour
        top_pads = [p for p in result.pcb.pads if p.layer == Layer.TOP]

        try:
            while True:
                # Corner tour (tension adjustment)
                print("\n=== コーナー巡回（テンション調整）===")
                for name, board_pt in corners:
                    machine_pt = board_transform.apply(board_pt)
                    print(
                        f"\n{name}: Board({board_pt.x:.1f}, {board_pt.y:.1f}) -> "
                        f"Machine({machine_pt.x:.3f}, {machine_pt.y:.3f})"
                    )
                    print("何かキーを押すと次へ進みます...")
                    interactive_display_at_point(
                        result, machine_pt, name, window_name=WINDOW_NAME
                    )

                # Grid point tour
                print("\n=== グリッド交点巡回 ===")
                if top_pads:
                    current_pos = result.stage.get_position()
                    pad_centers_3d = [p.center.to3d() for p in top_pads]
                    sorted_centers = sort_by_nearest(
                        pad_centers_3d, current_pos.to2d().to3d()
                    )

                    for i, center_3d in enumerate(sorted_centers):
                        board_pt = center_3d.to2d()
                        machine_pt = board_transform.apply(board_pt)
                        label = f"Grid {i + 1}/{len(sorted_centers)}"
                        print(
                            f"\n{label}: Board({board_pt.x:.1f}, {board_pt.y:.1f}) -> "
                            f"Machine({machine_pt.x:.3f}, {machine_pt.y:.3f})"
                        )
                        print("何かキーを押すと次へ進みます...")
                        interactive_display_at_point(
                            result, machine_pt, label, window_name=WINDOW_NAME
                        )
                else:
                    print("グリッド交点がありません")

                print("\n=== サイクル完了。Top-Leftに戻ります。===")

        except KeyboardInterrupt:
            print("\n終了します。")


if __name__ == "__main__":
    main()
