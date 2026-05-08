#!/usr/bin/env python3
"""ペースト塗布の実機運用スクリプト.

ボード計測ベースのフロー:
1. setup_board_calibration() で初期化〜Board変換計測
2. HeightPlaneMeasurer.measure() でその場で高さ計測
3. TOPレイヤーのパッドを取得・ソート
4. PasteApplicator作成
5. 任意で対話的ローディング → リトラクション → パッド中心にポイント塗布
"""

import argparse
import logging
from pathlib import Path

from pcb_assembly import gcode
from pcb_assembly.config import get_machine_config
from pcb_assembly.control.adjust import HeightPlaneMeasurer
from pcb_assembly.control.pasting import PasteApplicator, interactive_loading
from pcb_assembly.control.probe import ProbeExecutor
from pcb_assembly.control.setup import machine_session, setup_board_calibration
from pcb_assembly.geometry import Compose, Move, sort_by_nearest
from pcb_assembly.hal import PasteDispenser, Probe
from pcb_assembly.pcb import Layer
from pcb_assembly.utils import setup_logging

WINDOW_NAME = "Paste Solder"


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

    # 初期化（machine_session の外）
    machine = get_machine_config(args.machine)
    result = setup_board_calibration(
        machine=machine,
        pcb_file_path=args.pcb_file,
        tolerance=args.tolerance,
        window_name=WINDOW_NAME,
    )
    klipper = result.klipper
    stage = result.stage
    machine = result.machine
    board_transform = result.board_transform

    # TOPレイヤーの銅箔・パッドを取得
    top_coppers = [c for c in result.pcb.copper if c.layer == Layer.TOP]
    top_pads = [p for p in result.pcb.pads if p.layer == Layer.TOP]
    toolhead_offset = machine.paste_dispenser.toolhead.to_transform()

    # Probe / HeightPlaneMeasurer初期化
    probe_config = machine.probe
    probe = Probe(
        klipper.readonly,
        servo_name=probe_config.servo_name,
        revolution_distance=probe_config.revolution_distance,
        down_distance=probe_config.down_distance,
    )
    probe_executor = ProbeExecutor(klipper=klipper, probe=probe, stage=stage)
    height_measurer = HeightPlaneMeasurer(
        probe_executor=probe_executor,
        klipper=klipper,
        stage=stage,
        min_radius=probe_config.min_radius,
        min_samples=probe_config.min_samples,
        max_samples=probe_config.max_samples,
    )

    # PasteDispenser初期化
    dispenser_config = machine.paste_dispenser
    paste_dispenser = PasteDispenser(
        klipper=klipper.readonly,
        rotations_per_ul=dispenser_config.rotations_per_ul,
    )

    # 実行（machine_session 内）
    with machine_session(klipper):
        try:
            # Height plane計測
            print("\n=== Height plane計測 ===")
            height_plane = height_measurer.measure(
                coppers=top_coppers,
                board_to_machine=Compose([board_transform, toolhead_offset]),
            )

            # nearest-neighborソート
            current_pos = stage.get_position()
            pad_centers_3d = [p.center.to3d() for p in top_pads]
            sorted_centers = sort_by_nearest(pad_centers_3d, current_pos.to2d().to3d())
            center_to_pad = {p.center.to3d(): p for p in top_pads}
            sorted_pads = [center_to_pad[c] for c in sorted_centers]

            # board→machine全変換 (board_transform + toolhead_offset + height_plane)
            transform = Compose([board_transform, toolhead_offset, height_plane])

            # PasteApplicator作成
            with PasteApplicator(
                klipper=klipper,
                paste_dispenser=paste_dispenser,
                stage=stage,
                nozzle_diameter=dispenser_config.nozzle_diameter,
                dispense_rate=dispenser_config.dispense_rate,
                dispense_accel=dispenser_config.dispense_accel,
                ul_per_mm2=dispenser_config.ul_per_mm2,
                retraction=dispenser_config.retract_amount,
                retraction_rate=dispenser_config.retract_rate,
                retraction_accel_factor=dispenser_config.retract_accel_factor,
                paste_height=dispenser_config.paste_height,
                prime_extra_delay=dispenser_config.prime_extra_delay,
                transform=transform,
            ) as applicator:
                # 対話的ローディング
                if args.interactive_loading:
                    pos = stage.get_position()
                    klipper.send_gcode(stage.to_gcode(Move(0, 0, 0)))
                    interactive_loading(applicator, args.amount)
                    klipper.send_gcode(
                        stage.to_gcode(Move.from_point(pos)) + gcode.wait_for_done()
                    )

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
            klipper.emergency_stop()


if __name__ == "__main__":
    main()
