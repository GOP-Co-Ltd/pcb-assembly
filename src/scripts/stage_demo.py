#!/usr/bin/env python3
"""XYZステージの動作確認スクリプト."""

import argparse

from pcb_assembly.hal.klipper import Klipper
from pcb_assembly.hal.stage import XYZStage


def main() -> None:
    parser = argparse.ArgumentParser(description="XYZステージの動作確認")
    parser.add_argument(
        "-H", "--host", type=str, default="localhost", help="Moonrakerホスト"
    )
    parser.add_argument("-p", "--port", type=int, default=7125, help="Moonrakerポート")
    parser.add_argument(
        "-s", "--speed", type=float, default=50.0, help="デフォルト移動速度 (mm/s)"
    )
    args = parser.parse_args()

    klipper = Klipper(host=args.host, port=args.port)
    stage = XYZStage(klipper, default_speed=args.speed)

    print("XYZステージデモ")
    print("コマンド: home, move, moverel, pos, limits, present, quit")
    print()

    while True:
        cmd = input("> ").strip().lower()
        if not cmd:
            continue

        match cmd:
            case "quit" | "q":
                break
            case "home":
                print("ホーミング中...")
                stage.home()
                print("完了")
            case "move":
                try:
                    x_str = input("  X (空欄でスキップ): ").strip()
                    y_str = input("  Y (空欄でスキップ): ").strip()
                    z_str = input("  Z (空欄でスキップ): ").strip()
                    speed_str = input("  速度 mm/s (空欄でデフォルト): ").strip()

                    x = float(x_str) if x_str else None
                    y = float(y_str) if y_str else None
                    z = float(z_str) if z_str else None
                    speed = float(speed_str) if speed_str else None

                    print("移動中...")
                    stage.move(x=x, y=y, z=z, speed=speed)
                    print("完了")
                except ValueError:
                    print("無効な値です")
            case "moverel":
                try:
                    x_str = input("  X (空欄でスキップ): ").strip()
                    y_str = input("  Y (空欄でスキップ): ").strip()
                    z_str = input("  Z (空欄でスキップ): ").strip()
                    speed_str = input("  速度 mm/s (空欄でデフォルト): ").strip()

                    x = float(x_str) if x_str else None
                    y = float(y_str) if y_str else None
                    z = float(z_str) if z_str else None
                    speed = float(speed_str) if speed_str else None

                    print("相対移動中...")
                    stage.move(x=x, y=y, z=z, speed=speed, relative=True)
                    print("完了")
                except ValueError:
                    print("無効な値です")
            case "pos":
                position = stage.get_position()
                print(f"  X={position.x:.2f} Y={position.y:.2f} Z={position.z:.2f}")
            case "limits":
                limits = stage.get_limits()
                print(f"  X: {limits.x.min:.2f} ~ {limits.x.max:.2f}")
                print(f"  Y: {limits.y.min:.2f} ~ {limits.y.max:.2f}")
                print(f"  Z: {limits.z.min:.2f} ~ {limits.z.max:.2f}")
            case "present":
                print("プレゼント位置へ移動中...")
                stage.present()
                print("完了")
            case _:
                print("不明なコマンドです")

    print("終了")


if __name__ == "__main__":
    main()
