#!/usr/bin/env python3
"""はんだペーストローディングスクリプト.

PasteApplicator を使ってペーストを対話的にローディングする。
"""

import argparse
import logging

from pcb_assembly.config import get_machine_config
from pcb_assembly.control.pasting import PasteApplicator, interactive_loading
from pcb_assembly.hal import Klipper, PasteDispenser, XYZStage
from pcb_assembly.utils import setup_logging


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(description="はんだペーストローディングスクリプト")
    parser.add_argument(
        "--machine",
        "-m",
        type=str,
        default="pd_china_frame",
        help="マシン名",
    )
    parser.add_argument(
        "--amount",
        "-a",
        type=float,
        default=10.0,
        help="デフォルトの押し出し量 [μL]",
    )
    args = parser.parse_args()

    machine = get_machine_config(args.machine)
    klipper_config = machine.klipper
    dispenser_config = machine.paste_dispenser

    klipper = Klipper(host=klipper_config.host, port=klipper_config.port)
    paste_dispenser = PasteDispenser(
        klipper=klipper.readonly,
        rotations_per_ul=dispenser_config.rotations_per_ul,
    )
    stage = XYZStage(klipper.readonly)
    with PasteApplicator(
        klipper=klipper,
        paste_dispenser=paste_dispenser,
        stage=stage,
        nozzle_size=dispenser_config.nozzle_size,
        dispense_rate=dispenser_config.dispense_rate,
        dispense_accel=dispenser_config.dispense_accel,
        ul_per_mm2=dispenser_config.ul_per_mm2,
        retraction=dispenser_config.retract_amount,
        retraction_rate=dispenser_config.retract_rate,
        retraction_accel_factor=dispenser_config.retract_accel_factor,
        paste_height=dispenser_config.paste_height,
        prime_extra_delay=dispenser_config.prime_extra_delay,
    ) as applicator:
        interactive_loading(applicator, args.amount)


if __name__ == "__main__":
    main()
