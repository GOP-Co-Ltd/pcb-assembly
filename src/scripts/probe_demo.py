#!/usr/bin/env python3
"""プローブの動作確認スクリプト."""

import argparse

from pcb_assembly.hal.probe import Probe


def main() -> None:
    parser = argparse.ArgumentParser(description="プローブの動作確認")
    parser.add_argument("-a", "--a-pin", type=int, default=17, help="A相のGPIOピン番号")
    parser.add_argument("-b", "--b-pin", type=int, default=27, help="B相のGPIOピン番号")
    parser.add_argument(
        "-d",
        "--rotation-distance",
        type=float,
        default=40.0,
        help="1回転あたりの移動距離(mm)",
    )
    parser.add_argument(
        "-p",
        "--rotation-pulse",
        type=int,
        default=600,
        help="1回転あたりのパルス数(PPR)",
    )
    parser.add_argument("-i", "--inverse", action="store_true", help="回転方向を反転")
    args = parser.parse_args()

    probe = Probe(
        a_pin=args.a_pin,
        b_pin=args.b_pin,
        rotation_distance=args.rotation_distance,
        rotation_pulse=args.rotation_pulse,
        inverse=args.inverse,
    )

    print("プローブデモ")
    print("Ctrl+C で終了")
    print()

    try:
        while True:
            input("Enterで計測開始...")
            with probe:
                input("計測中... Enterで終了")
            result = probe.result()
            print(f"  min: {result.min:.2f}mm, max: {result.max:.2f}mm")
            print()
    except KeyboardInterrupt:
        print("\n終了")


if __name__ == "__main__":
    main()
