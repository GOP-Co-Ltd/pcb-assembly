#!/usr/bin/env python3
"""ペースト塗布の実機運用スクリプト.

ボード計測ベースのフロー:
1. PasteSession.setup() で初期化〜Board変換計測〜塗布用HAL構築
2. HeightPlaneMeasurer.measure() でその場で高さ計測
3. TOPレイヤーのパッドを取得・ソート
4. PasteApplicator作成
5. 任意で対話的ローディング → リトラクション → パッド中心にポイント塗布
"""

import argparse
import logging
from pathlib import Path

from pcbasm import gcode
from pcbasm.geometry import Compose, HeightPlane, sort_by_nearest
from pcbasm.pasting import interactive_loading
from pcbasm.pcb import Copper, Layer, Pad
from pcbasm.session import PasteSession
from pcbasm.utils import setup_logging

WINDOW_NAME = "Paste Solder"


def _measure_height(session: PasteSession, top_coppers: list[Copper]) -> HeightPlane:
    """height_measurer.measure() を実行して HeightPlane を返す."""
    print("\n=== Height plane計測 ===")
    return session.height_measurer.measure(
        coppers=top_coppers,
        board_to_machine=session.board_to_machine,
        outline=session.pcb.outline.polygon,
    )


def _load_and_apply(
    session: PasteSession,
    height_plane: HeightPlane,
    top_pads: list[Pad],
    args: argparse.Namespace,
) -> None:
    """Nearest ソート → PasteApplicator 構築 → 任意ローディング → リトラクション → 塗布."""
    stage = session.stage

    # nearest-neighborソート
    current_pos = stage.get_position()
    pad_centers_3d = [p.center.to3d() for p in top_pads]
    sorted_centers = sort_by_nearest(pad_centers_3d, current_pos.to2d().to3d())
    center_to_pad = {p.center.to3d(): p for p in top_pads}
    sorted_pads = [center_to_pad[c] for c in sorted_centers]

    # board→machine全変換 (board_transform + toolhead_offset + height_plane)
    transform = Compose(
        [session.board_transform, session.toolhead_offset, height_plane]
    )

    # PasteApplicator作成
    with session.make_applicator(transform=transform) as applicator:
        # 対話的ローディング
        if args.interactive_loading:
            pos = stage.get_position()
            session.klipper.send_gcode(stage.move(x=0, y=0, z=0))
            interactive_loading(applicator, args.amount)
            session.klipper.send_gcode(
                stage.move(x=pos.x, y=pos.y, z=pos.z) + gcode.wait_for_done()
            )

        # リトラクション
        print("\n=== リトラクション ===")
        applicator.retract()
        print("リトラクション完了")

        # パッド中心にポイント塗布
        print(f"\n=== パッド塗布 ({len(sorted_pads)} パッド) ===")
        applicator.apply([pad.polygon for pad in sorted_pads])
        print("塗布完了")


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(description="ペースト塗布の実機運用スクリプト")
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
    parser.add_argument(
        "--amount",
        type=float,
        default=0.1,
        help="ローディング時のデフォルト押し出し量 [uL]",
    )
    parser.add_argument(
        "--interactive-loading",
        "-l",
        action="store_true",
        help="指定時のみ対話的ローディングを実行する",
    )
    args = parser.parse_args()

    with PasteSession.setup(
        machine_name=args.machine,
        pcb_file_path=args.pcb_file,
        tolerance=args.tolerance,
        window_name=WINDOW_NAME,
    ) as session:
        # TOPレイヤーの銅箔・パッドを取得
        top_coppers = [c for c in session.pcb.copper if c.layer == Layer.TOP]
        top_pads = [p for p in session.pcb.pads if p.layer == Layer.TOP]

        try:
            height_plane = _measure_height(session, top_coppers)
            _load_and_apply(session, height_plane, top_pads, args)
        except KeyboardInterrupt:
            print("\n=== 中止 ===")
            session.klipper.send_gcode(gcode.relax())


if __name__ == "__main__":
    main()
