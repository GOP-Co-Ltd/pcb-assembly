#!/usr/bin/env python3
"""プローブによる高さ計測のデモスクリプト.

指定の機械座標に移動した後、プローブマクロを実行してbed meshを計測する。
"""

import argparse
import logging
from pathlib import Path

from shapely import box

from pcb_assembly import gcode
from pcb_assembly.config import Machine
from pcb_assembly.control.adjust import HeightTransformMeasurer
from pcb_assembly.geometry import Identity, Point2d
from pcb_assembly.hal import Klipper, ProbeSensor, XYZStage
from pcb_assembly.pcb import Outline
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
    parser.add_argument("--board-width", type=float, default=100.0, help="基板幅 (mm)")
    parser.add_argument(
        "--board-height", type=float, default=100.0, help="基板高さ (mm)"
    )
    args = parser.parse_args()

    machine = Machine(args.config)
    klipper_config = machine.klipper

    klipper = Klipper(host=klipper_config.host, port=klipper_config.port)
    stage = XYZStage(klipper.readonly)
    probe = ProbeSensor(klipper.readonly)

    measurer = HeightTransformMeasurer(probe=probe, klipper=klipper, stage=stage)
    toolhead = machine.toolhead.to_transform()
    outline = Outline(polygon=box(0, 0, args.board_width, args.board_height))

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
    height_map = measurer.measure(outline=outline, board_to_machine=Identity())
    print(f"計測結果: {height_map}")

    klipper.send_gcode(gcode.relax())


if __name__ == "__main__":
    main()
