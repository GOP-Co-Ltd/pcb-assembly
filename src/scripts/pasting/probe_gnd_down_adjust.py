#!/usr/bin/env python3
"""probe_gnd ダウン距離調整スクリプト.

ProbeGround を使い、グラウンドピンのダウン距離 [mm] を対話的に入力して サーボを動かす。machine.toml の
[probe] down_distance を実機で詰めるための調整用。 終了時には必ずダウン距離 0（角度 0）へ戻す。
"""

import argparse
import logging

from pcbasm import gcode
from pcbasm.config import get_machine_config
from pcbasm.hal import Klipper, ProbeGround
from pcbasm.utils import setup_logging

logger = logging.getLogger(__name__)


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(description="probe_gnd ダウン距離調整スクリプト")
    parser.add_argument(
        "--machine",
        "-m",
        type=str,
        default="kurousagi",
        help="マシン名",
    )
    args = parser.parse_args()

    machine = get_machine_config(args.machine)
    klipper_config = machine.klipper
    probe_config = machine.probe

    klipper = Klipper(host=klipper_config.host, port=klipper_config.port)
    ground = ProbeGround(
        klipper.readonly,
        probe_config.servo_name,
        probe_config.revolution_distance,
    )

    logger.info("probe_gnd ダウン距離調整 (servo=%s)", probe_config.servo_name)
    logger.info("数値: その距離[mm]まで下げる, q: 終了 (終了時にダウン距離0へ戻す)")

    try:
        while True:
            cmd = input("down distance [mm] > ").strip()

            match cmd:
                case "q" | "quit":
                    break
                case _:
                    try:
                        distance = float(cmd)
                    except ValueError:
                        logger.warning("不正な入力です")
                        continue
                    logger.info("ダウン: %s mm", distance)
                    klipper.send_gcode(ground.down(distance) + gcode.wait_for_done())
    finally:
        logger.info("ダウン距離0へ戻します")
        klipper.send_gcode(ground.down(0.0) + gcode.wait_for_done())


if __name__ == "__main__":
    main()
