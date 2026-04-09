#!/usr/bin/env python3
"""ペースト塗布のデモスクリプト.

ボード計測ベースのフロー:
1. setup_board_calibration() で初期化〜Board変換計測
2. HeightMap読み込み
3. ボード中央座標を計算 → toolheadオフセット適用で機械座標に変換
4. PasteApplicator作成
5. ローディング → リトラクション → 円塗布
"""

import argparse
import logging
from pathlib import Path

from shapely import Point as ShapelyPoint

from pcb_assembly.config import Machine
from pcb_assembly.control.pasting import PasteApplicator, interactive_loading
from pcb_assembly.control.setup import machine_session, setup_board_calibration
from pcb_assembly.geometry import Compose, HeightMap, Point2d
from pcb_assembly.hal import NOZZLE_SPECS, PasteDispenser
from pcb_assembly.utils import setup_logging

PROJECT_ROOT = Path(__file__).parent.parent.parent
WINDOW_NAME = "Pasting Demo"


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(description="ペースト塗布デモ")
    parser.add_argument(
        "--config",
        "-c",
        type=Path,
        default=PROJECT_ROOT / "configs" / "pd_china_frame" / "machine.toml",
        help="設定ファイルのパス",
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
    parser.add_argument("--radius", type=float, default=3.0, help="塗布円の半径 [mm]")
    parser.add_argument(
        "--height-map",
        "-H",
        type=Path,
        required=True,
        help="HeightMapのJSONファイルパス",
    )
    args = parser.parse_args()

    machine = Machine(args.config)
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
        outline = result.pcb.outline

        board_transform = result.board_transform

        try:
            # HeightMap読み込み
            print("\n=== HeightMap読み込み ===")
            height_transform = HeightMap.load(args.height_map)
            print(f"HeightMap: {args.height_map}")

            # ボード中央座標を計算
            board_center = Point2d(outline.width / 2, outline.height / 2)
            toolhead_offset = machine.paste_dispenser.toolhead.to_transform()
            center_machine = Compose([board_transform, toolhead_offset]).apply(
                board_center
            )
            print(
                f"ボード中央 (機械座標): X={center_machine.x:.3f}, Y={center_machine.y:.3f}"
            )

            # PasteDispenser/nozzle_spec初期化
            dispenser_config = machine.paste_dispenser
            paste_dispenser = PasteDispenser(
                klipper=klipper.readonly,
                rotations_per_ul=dispenser_config.rotations_per_ul,
            )
            nozzle_spec = NOZZLE_SPECS[dispenser_config.nozzle_size]

            # PasteApplicator作成
            with PasteApplicator(
                klipper=klipper,
                paste_dispenser=paste_dispenser,
                stage=stage,
                nozzle_spec=nozzle_spec,
                paste_velocity=dispenser_config.paste_velocity,
                paste_thickness=dispenser_config.paste_thickness,
                retraction=dispenser_config.retract_amount,
                retraction_rate=dispenser_config.retract_rate,
                retraction_accel_factor=dispenser_config.retract_accel_factor,
                paste_accel=dispenser_config.dispense_accel,
                paste_height=dispenser_config.paste_height,
                transform=height_transform,
            ) as applicator:
                # 対話的ローディング
                interactive_loading(applicator, args.amount)

                # リトラクション
                print("\n=== リトラクション ===")
                applicator.retract()
                print("リトラクション完了")

                # 円塗布
                print(f"\n=== 円塗布 (半径: {args.radius} mm) ===")
                circle = ShapelyPoint(center_machine.x, center_machine.y).buffer(
                    args.radius
                )
                applicator.apply([circle])
                print("塗布完了")

        except KeyboardInterrupt:
            print("\n=== 緊急停止 ===")
            result.klipper.emergency_stop()


if __name__ == "__main__":
    main()
