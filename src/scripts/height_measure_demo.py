#!/usr/bin/env python3
"""プローブによる高さ計測のデモスクリプト.

指定の機械座標に移動した後、プローブマクロを実行して高さを計測する。
"""

import argparse
import logging
from pathlib import Path

from pcb_assembly import gcode
from pcb_assembly.config import Machine
from pcb_assembly.control.adjust import HeightTransformMeasurer
from pcb_assembly.geometry import Point2d
from pcb_assembly.hal import Klipper, XYZStage
from pcb_assembly.hal.probe import ProbeSensor
from pcb_assembly.utils import setup_logging

PROJECT_ROOT = Path(__file__).parent.parent.parent


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(description="プローブ高さ計測デモ")
    parser.add_argument(
        "--config",
        type=str,
        default=str(PROJECT_ROOT / "configs" / "pd_china_frame" / "machine.toml"),
        help="設定ファイルのパス",
    )
    args = parser.parse_args()

    machine = Machine(args.config)
    klipper_config = machine.klipper
    probe_config = machine.probe

    klipper = Klipper(host=klipper_config.host, port=klipper_config.port)
    stage = XYZStage(klipper.readonly)
    probe = ProbeSensor(
        a_pin=probe_config.a_pin,
        b_pin=probe_config.b_pin,
        rotation_distance=probe_config.rotation_distance,
        rotation_pulse=probe_config.rotation_pulse,
        inverse=probe_config.inverse,
    )

    measurer = HeightTransformMeasurer(probe=probe, klipper=klipper)
    toolhead = machine.toolhead.to_transform()

    # ホーミング
    print("=== ホーミング ===")
    klipper.send_gcode(gcode.homing(x=True, y=True, z=True) + gcode.wait_for_done())
    print("ホーミング完了")

    # 対話ループ（位置合わせ）
    print("\n座標を入力して位置を合わせます。計測するには 'q' を入力してください。")
    while True:
        try:
            raw = input("\nX Y (mm) > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if raw.lower() == "q":
            break
        parts = raw.split()
        if len(parts) != 2:
            print("X と Y をスペース区切りで入力してください（例: 100 200）")
            continue
        try:
            x, y = float(parts[0]), float(parts[1])
        except ValueError:
            print("数値を入力してください")
            continue

        target = toolhead.apply(Point2d(x, y))
        print(f"=== 移動: X={target.x}, Y={target.y} (toolheadオフセット適用) ===")
        klipper.send_gcode(
            gcode.move(x=target.x, y=target.y, velocity=stage.max_velocity)
            + gcode.wait_for_done()
        )

    # 高さ計測
    print("\n=== 高さ計測 ===")
    transform = measurer.measure()
    print(f"計測結果: {transform}")

    klipper.send_gcode(gcode.relax())


if __name__ == "__main__":
    main()
