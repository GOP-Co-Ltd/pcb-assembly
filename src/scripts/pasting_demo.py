#!/usr/bin/env python3
"""ペースト塗布のデモスクリプト.

ボード計測ベースのフロー:
1. setup_board_calibration() で初期化〜Board変換計測
2. HeightMap読み込み
3. TOPレイヤーのパッドを取得・ソート
4. PasteApplicator作成
5. ローディング → リトラクション → パッド中心にポイント塗布
"""

import argparse
import logging
from pathlib import Path

from pcb_assembly.config import get_machine_config
from pcb_assembly.control.pasting import PasteApplicator, interactive_loading
from pcb_assembly.control.setup import machine_session, setup_board_calibration
from pcb_assembly.geometry import Compose, HeightMap, sort_by_nearest
from pcb_assembly.hal import PasteDispenser
from pcb_assembly.pcb import Layer
from pcb_assembly.utils import setup_logging

WINDOW_NAME = "Pasting Demo"


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(description="ペースト塗布デモ")
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
        "--amount", type=float, default=10.0, help="デフォルトの押し出し量 [uL]"
    )
    parser.add_argument(
        "--height-map",
        "-H",
        type=Path,
        required=True,
        help="HeightMapのJSONファイルパス",
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
        klipper = result.klipper
        stage = result.stage
        machine = result.machine

        board_transform = result.board_transform

        try:
            # HeightMap読み込み
            print("\n=== HeightMap読み込み ===")
            height_transform = HeightMap.load(args.height_map)
            print(f"HeightMap: {args.height_map}")

            # TOPレイヤーのパッドを取得
            top_pads = [p for p in result.pcb.pads if p.layer == Layer.TOP]
            print(f"TOPレイヤーのパッド数: {len(top_pads)}")

            # nearest-neighborソート
            current_pos = stage.get_position()
            pad_centers_3d = [p.center.to3d() for p in top_pads]
            sorted_centers = sort_by_nearest(pad_centers_3d, current_pos.to2d().to3d())
            center_to_pad = {p.center.to3d(): p for p in top_pads}
            sorted_pads = [center_to_pad[c] for c in sorted_centers]

            # board→machine全変換 (board_transform + toolhead_offset + HeightMap)
            toolhead_offset = machine.paste_dispenser.toolhead.to_transform()
            transform = Compose([board_transform, toolhead_offset, height_transform])

            # PasteDispenser初期化
            dispenser_config = machine.paste_dispenser
            paste_dispenser = PasteDispenser(
                klipper=klipper.readonly,
                rotations_per_ul=dispenser_config.rotations_per_ul,
            )

            # PasteApplicator作成
            with PasteApplicator(
                klipper=klipper,
                paste_dispenser=paste_dispenser,
                stage=stage,
                nozzle_size=dispenser_config.nozzle_size,
                dispense_accel=dispenser_config.dispense_accel,
                ul_per_mm2=dispenser_config.ul_per_mm2,
                retraction=dispenser_config.retract_amount,
                retraction_rate=dispenser_config.retract_rate,
                retraction_accel_factor=dispenser_config.retract_accel_factor,
                paste_height=dispenser_config.paste_height,
                transform=transform,
            ) as applicator:
                # 対話的ローディング
                interactive_loading(applicator, args.amount)

                # リトラクション
                print("\n=== リトラクション ===")
                applicator.retract()
                print("リトラクション完了")

                # パッド中心にポイント塗布
                print(f"\n=== パッド塗布 ({len(sorted_pads)} パッド) ===")
                applicator.apply([pad.polygon for pad in sorted_pads])
                print("塗布完了")

        except KeyboardInterrupt:
            print("\n=== 緊急停止 ===")
            result.klipper.emergency_stop()


if __name__ == "__main__":
    main()
