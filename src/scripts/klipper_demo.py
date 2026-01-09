#!/usr/bin/env python3
"""Klipperの動作確認スクリプト."""

import argparse

from pcb_assembly.hal.klipper import Klipper


def main() -> None:
    parser = argparse.ArgumentParser(description="Klipperの動作確認")
    parser.add_argument(
        "-H", "--host", type=str, default="localhost", help="Moonrakerホスト"
    )
    parser.add_argument("-p", "--port", type=int, default=7125, help="Moonrakerポート")
    args = parser.parse_args()

    klipper = Klipper(host=args.host, port=args.port)

    print("Klipperデモ")
    print("コマンド: home, relax, move, pos, status, gcode, macros, config, quit")
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
                klipper.home()
                print("完了")
            case "relax":
                print("モーターをリラックス中...")
                klipper.relax()
                print("完了")
            case "move":
                try:
                    x = float(input("  X: ").strip())
                    y = float(input("  Y: ").strip())
                    z = float(input("  Z: ").strip())
                    print("移動中...")
                    klipper.send_gcode(f"G0 X{x} Y{y} Z{z}")
                    klipper.wait_for_move()
                    print("完了")
                except ValueError:
                    print("無効な値です")
            case "pos":
                position = klipper.get_status("gcode_move", "gcode_position")
                print(f"  X={position[0]:.2f} Y={position[1]:.2f} Z={position[2]:.2f}")
            case "status":
                obj = input("  object: ").strip()
                attr = input("  attribute: ").strip()
                value = klipper.get_status(obj, attr)
                print(f"  {value}")
            case "gcode":
                gcode = input("  G-code: ").strip()
                klipper.send_gcode(gcode)
                klipper.wait_for_move()
                print("完了")
            case "macros":
                macros = klipper.get_macros()
                for name, macro in sorted(macros.items()):
                    desc = f" - {macro.description}" if macro.description else ""
                    print(f"  {name}{desc}")
                    if macro.variables:
                        for var_name, var_value in macro.variables.items():
                            print(f"    {var_name}: {var_value}")
            case "config":
                config = klipper.get_config()
                for section, values in sorted(config.items()):
                    print(f"  [{section}]")
                    for key, value in values.items():
                        print(f"    {key}: {value}")
            case _:
                print("不明なコマンドです")

    print("終了")


if __name__ == "__main__":
    main()
