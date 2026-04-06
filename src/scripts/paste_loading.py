#!/usr/bin/env python3
"""はんだペーストローディングスクリプト.

AirPump と ManualStepper (paste_dispenser) を使ってペーストをローディングする対話型スクリプト。
"""

import argparse
import logging

from pcb_assembly.hal import AirPump, ManualStepper
from pcb_assembly.hal.klipper import Klipper
from pcb_assembly.utils import setup_logging


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(description="はんだペーストローディングスクリプト")
    parser.add_argument(
        "-H", "--host", type=str, default="localhost", help="Moonrakerホスト"
    )
    parser.add_argument("--port", type=int, default=7125, help="Moonrakerポート")
    parser.add_argument(
        "--rotations",
        "-r",
        type=float,
        default=10,
        help="デフォルト回転数 (回転)",
    )
    parser.add_argument(
        "--rpm",
        type=float,
        default=60.0,
        help="回転速度 (RPM)",
    )
    parser.add_argument(
        "--accel-factor",
        type=float,
        default=5.0,
        help="加速度係数 (accel = speed_mm_s × factor)",
    )
    args = parser.parse_args()

    klipper = Klipper(host=args.host, port=args.port)
    air_pump = AirPump(klipper.readonly)
    stepper = ManualStepper(klipper.readonly, "paste_dispenser")

    klipper.send_gcode(air_pump.on())
    klipper.send_gcode(stepper.enable())

    speed_deg_s = args.rpm * 6  # RPM → deg/s
    accel = speed_deg_s * args.accel_factor

    print(
        f"回転速度: {args.rpm} RPM ({speed_deg_s:.1f} deg/s), 加速度係数: {args.accel_factor}"
    )
    print("Enterで回転、回転数を入力して変更可、q/quit で終了")

    try:
        while True:
            raw = input("> ").strip()
            if raw in ("q", "quit"):
                break
            if raw == "":
                rotations = args.rotations
            else:
                try:
                    rotations = float(raw)
                except ValueError:
                    print("不正な入力")
                    continue
            angle = rotations * 360
            klipper.send_gcode(
                stepper.reset_position() + stepper.rotate(angle, speed_deg_s, accel)
            )
    finally:
        klipper.send_gcode(air_pump.off())
        klipper.send_gcode(stepper.disable())


if __name__ == "__main__":
    main()
