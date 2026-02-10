#!/usr/bin/env python3
"""ペーストディスペンサーのローディングデモスクリプト."""

import argparse
import logging
from pathlib import Path

from pcb_assembly import gcode
from pcb_assembly.config import Machine
from pcb_assembly.control.adjust import HeightTransformMeasurer
from pcb_assembly.control.dispensing import PasteLoader
from pcb_assembly.geometry import Point2d, Point3d
from pcb_assembly.hal import Klipper, PasteDispenser, ProbeSensor, XYZStage
from pcb_assembly.utils import setup_logging

PROJECT_ROOT = Path(__file__).parent.parent.parent


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(
        description="ペーストディスペンサーのローディングデモ"
    )
    parser.add_argument(
        "--config",
        type=str,
        default=str(PROJECT_ROOT / "configs" / "pd_china_frame" / "machine.toml"),
        help="設定ファイルのパス",
    )
    parser.add_argument(
        "--amount",
        type=float,
        default=1.0,
        help="デフォルトの押し出し量 [μL]",
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=10.0,
        help="ローディング速度 [μL/sec]",
    )
    parser.add_argument(
        "--accel",
        type=float,
        default=10.0,
        help="ローディング加速度 [μL/sec²]",
    )
    parser.add_argument(
        "--retract",
        type=float,
        default=10.0,
        help="リトラクション量 [μL]",
    )
    parser.add_argument("x", type=float, help="高さ計測位置のX座標（mm）")
    parser.add_argument("y", type=float, help="高さ計測位置のY座標（mm）")
    args = parser.parse_args()

    machine = Machine(args.config)
    klipper_config = machine.klipper
    dispenser_config = machine.paste_dispenser

    klipper = Klipper(host=klipper_config.host, port=klipper_config.port)
    stage = XYZStage(klipper.readonly)
    paste_dispenser = PasteDispenser(
        klipper=klipper.readonly,
        syringe_size=dispenser_config.syringe_size,
    )
    loader = PasteLoader(
        klipper=klipper,
        paste_dispenser=paste_dispenser,
        rate=args.rate,
        accel=args.accel,
    )

    probe_config = machine.probe
    probe = ProbeSensor(
        a_pin=probe_config.a_pin,
        b_pin=probe_config.b_pin,
        rotation_distance=probe_config.rotation_distance,
        rotation_pulse=probe_config.rotation_pulse,
        inverse=probe_config.inverse,
    )
    measurer = HeightTransformMeasurer()
    measurer.validate_klipper(klipper)

    print(f"ディスペンシングデモ (デフォルト量: {args.amount} μL)")
    print("Enter: デフォルト量を押し出し, 数値: その量を押し出し, q: 終了")
    print()

    while True:
        cmd = input("> ").strip()

        match cmd:
            case "q" | "quit":
                break
            case "":
                print(f"ローディング: {args.amount} μL")
                loader.load(args.amount)
                print("完了")
            case _:
                try:
                    amount = float(cmd)
                except ValueError:
                    print("不正な入力です")
                    continue
                print(f"ローディング: {amount} μL")
                loader.load(amount)
                print("完了")

    # リトラクション
    print(f"\n=== リトラクション: {args.retract} μL ===")
    loader.load(-args.retract)
    print("リトラクション完了")

    # ホーミング
    print("\n=== ホーミング ===")
    klipper.send_gcode(gcode.homing(x=True, y=True, z=True) + gcode.wait_for_done())
    print("ホーミング完了")

    # 指定座標にToolheadオフセットを加えて移動
    toolhead = machine.toolhead.to_transform()
    target = toolhead.apply(Point2d(args.x, args.y))
    print(f"\n=== 移動: X={target.x}, Y={target.y} (toolheadオフセット適用) ===")
    klipper.send_gcode(
        gcode.move(x=target.x, y=target.y, velocity=30) + gcode.wait_for_done()
    )

    # 高さ計測
    print("\n=== 高さ計測 ===")
    transform = measurer.measure(probe, klipper)
    # 計測高さ+10mm上で待機
    print("\n=== 10 mm 上空に移動して待機 ===")
    transformed_pos = transform.apply(Point3d.zero(z=10.0))
    klipper.send_gcode(
        gcode.move(z=transformed_pos.z, velocity=stage.max_velocity)
        + gcode.wait_for_done()
    )

    klipper.send_gcode(gcode.relax())
    print("終了")


if __name__ == "__main__":
    main()
