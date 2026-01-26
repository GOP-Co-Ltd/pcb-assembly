#!/usr/bin/env python3
"""XYZステージの状態確認スクリプト."""

import argparse

from pcb_assembly.hal.klipper import Klipper
from pcb_assembly.hal.stage import XYZStage


def main() -> None:
    parser = argparse.ArgumentParser(description="XYZステージの状態確認")
    parser.add_argument(
        "-H", "--host", type=str, default="localhost", help="Moonrakerホスト"
    )
    parser.add_argument("-p", "--port", type=int, default=7125, help="Moonrakerポート")
    args = parser.parse_args()

    klipper = Klipper(host=args.host, port=args.port)
    stage = XYZStage(klipper.readonly)

    print("=== XYZステージ状態 ===")
    print()

    position = stage.get_position()
    print("現在位置:")
    print(f"  X={position.x:.2f} Y={position.y:.2f} Z={position.z:.2f}")
    print()

    limits = stage.limits
    print("可動域:")
    print(f"  X: {limits.x.min:.2f} ~ {limits.x.max:.2f}")
    print(f"  Y: {limits.y.min:.2f} ~ {limits.y.max:.2f}")
    print(f"  Z: {limits.z.min:.2f} ~ {limits.z.max:.2f}")


if __name__ == "__main__":
    main()
