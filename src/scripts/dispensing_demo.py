#!/usr/bin/env python3
"""ペーストディスペンサーのローディングデモスクリプト."""

import argparse
import logging
from pathlib import Path

from pcb_assembly import gcode
from pcb_assembly.config import Machine
from pcb_assembly.control.dispensing import PasteLoader
from pcb_assembly.hal import Klipper, PasteDispenser
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
    args = parser.parse_args()

    machine = Machine(args.config)
    klipper_config = machine.klipper
    dispenser_config = machine.paste_dispenser

    klipper = Klipper(host=klipper_config.host, port=klipper_config.port)
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

    klipper.send_gcode(gcode.relax())
    print("終了")


if __name__ == "__main__":
    main()
