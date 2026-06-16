#!/usr/bin/env python3
"""はんだペーストディスペンサーの流量キャリブレーション.

ManualStepperを既知のN回転だけ駆動し、出てきたペーストの質量(mg)と
比重(水比重)から `rotations_per_ul` を算出する対話型CLI。
精度向上のため計測を複数回（デフォルト3回）繰り返し、質量の平均から算出する。

実行手順:
    1. interactive_loading でノズル先端までペーストを充填する
    2. 各計測ごとに（指定回数だけ繰り返す）:
        - はかりにキャッチ皿を置きタール (0g) する
        - N回転を実行
        - 計測した質量を入力
    3. 全計測の最後に自動でリトラクション
    4. ペーストの比重を入力
    5. 各回・平均・標準偏差と rotations_per_ul が出力されるので
       configs/{machine}/machine.toml に反映する
"""

import argparse
import logging

from pcbasm.config import get_machine_config
from pcbasm.hal import Klipper, PasteDispenser, XYZStage
from pcbasm.pasting import (
    FlowCalibrationSet,
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
    parser.add_argument("--count", "-c", type=int, default=3, help="計測回数 [回]")
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
    with PasteApplicator.from_config(
        klipper, paste_dispenser, stage, dispenser_config
    ) as applicator:
        interactive_loading(applicator, args.load_amount)

        count = max(1, args.count)
        masses_mg: list[float] = []
        for i in range(count):
            input(
                f"[{i + 1}/{count}] はかりにキャッチ皿を置き、"
                "タール (0g) にしたら Enter: "
            )
            applicator.calibrate(args.rotations, args.rate, args.accel)
            masses_mg.append(
                _prompt_positive_float(
                    f"[{i + 1}/{count}] ペーストが安定したら計測した質量 (mg) を入力: "
                )
            )

        applicator.retract()
        sg = _prompt_positive_float("ペーストの比重 (水比重, データシート値): ")

        result = FlowCalibrationSet(
            rotations=args.rotations,
            masses_mg=tuple(masses_mg),
            specific_gravity=sg,
        )
        per_ul = ", ".join(f"{c.rotations_per_ul:.4f}" for c in result.per_measurement)
        mass_list = ", ".join(f"{m:.1f}" for m in masses_mg)
        print("\n=== 結果 ===")
        print(
            f"rotations_per_ul = {result.rotations_per_ul:.6f} "
            f"± {result.stdev_rotations_per_ul:.6f} ({count} 回平均)"
        )
        print(f"  各回: {per_ul} rev/μL / 質量: {mass_list} mg")
        print(
            f"  configs/{args.machine}/machine.toml の "
            f"[paste_dispenser] rotations_per_ul を上記の値に更新してください"
        )


if __name__ == "__main__":
    main()
