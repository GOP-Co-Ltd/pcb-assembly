#!/usr/bin/env python3
"""はんだペーストディスペンサーの流量キャリブレーション.

ManualStepperを既知のN回転だけ駆動し、出てきたペーストの質量(mg)と
比重(水比重)から `rotations_per_ul` を算出する対話型CLI。

実行手順:
    1. interactive_loading でノズル先端までペーストを充填する
    2. はかりにキャッチ皿を置きタール (0g) する
    3. N回転を実行
    4. 計測した質量を入力
    5. 自動でリトラクション
    6. ペーストの比重を入力
    7. rotations_per_ul が出力されるので configs/{machine}/machine.toml に反映する
"""

import argparse
import logging

from pcbasm.config import get_machine_config
from pcbasm.hal import Klipper, PasteDispenser, XYZStage
from pcbasm.pasting import (
    FlowCalibration,
    PasteApplicator,
    interactive_loading,
)
from pcbasm.utils import setup_logging


def _parse_positive_float(raw: str) -> float | None:
    try:
        v = float(raw)
    except ValueError:
        return None
    return v if v > 0 else None


def _prompt_positive_float(message: str) -> float:
    while True:
        value = _parse_positive_float(input(message).strip())
        if value is not None:
            return value
        print("正の数値を入力してください")


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(
        description="はんだペーストディスペンサーの流量キャリブレーション"
    )
    parser.add_argument(
        "--machine", "-m", type=str, default="kurousagi", help="マシン名"
    )
    parser.add_argument(
        "--rotations", "-n", type=float, default=30, help="回転数 [rev]"
    )
    parser.add_argument(
        "--rate", "-r", type=float, default=5.0, help="角速度 [rev/sec]"
    )
    parser.add_argument(
        "--accel", "-a", type=float, default=10.0, help="角加速度 [rev/sec^2]"
    )
    parser.add_argument(
        "--load-amount",
        type=float,
        default=0.1,
        help="interactive_loading のデフォルト押し出し量 [μL]",
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
        nozzle_diameter=dispenser_config.nozzle_diameter,
        fill_speed=dispenser_config.fill_speed,
        max_dispense_rate=dispenser_config.max_dispense_rate,
        dispense_accel=dispenser_config.dispense_accel,
        ul_per_mm2=dispenser_config.ul_per_mm2,
        retraction=dispenser_config.retract_amount,
        retraction_rate=dispenser_config.retract_rate,
        retraction_accel_factor=dispenser_config.retract_accel_factor,
        paste_height=dispenser_config.paste_height,
        prime_extra_delay=dispenser_config.prime_extra_delay,
    ) as applicator:
        interactive_loading(applicator, args.load_amount)

        input("はかりにキャッチ皿を置き、タール (0g) にしたら Enter: ")
        applicator.calibrate(args.rotations, args.rate, args.accel)
        mass_mg = _prompt_positive_float(
            "ペーストが安定したら計測した質量 (mg) を入力: "
        )

        applicator.retract()
        sg = _prompt_positive_float("ペーストの比重 (水比重, データシート値): ")

        result = FlowCalibration(
            rotations=args.rotations, mass_mg=mass_mg, specific_gravity=sg
        )
        print("\n=== 結果 ===")
        print(f"rotations_per_ul = {result.rotations_per_ul:.6f}")
        print(
            f"  configs/{args.machine}/machine.toml の "
            f"[paste_dispenser] rotations_per_ul を上記の値に更新してください"
        )


if __name__ == "__main__":
    main()
