#!/usr/bin/env python3
"""基板表面のbed meshを計測し、HeightMapを保存するスクリプト.

処理順:
1. setup_board_calibration() で初期化〜Board変換計測
2. ProbeExecutor初期化
3. Bed mesh計測（HeightTransformMeasurer）
4. HeightMapをファイルに保存
"""

import argparse
import logging
from datetime import datetime
from pathlib import Path

from pcb_assembly.control.adjust import HeightTransformMeasurer
from pcb_assembly.control.probe import ProbeExecutor
from pcb_assembly.control.setup import machine_session, setup_board_calibration
from pcb_assembly.geometry import Compose
from pcb_assembly.hal import ProbeSensor
from pcb_assembly.utils import setup_logging

PROJECT_ROOT = Path(__file__).parent.parent.parent
WINDOW_NAME = "Height Mesh"


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(description="基板表面のbed mesh計測")
    parser.add_argument(
        "--config",
        "-c",
        type=Path,
        default=PROJECT_ROOT / "configs" / "pd_china_frame" / "machine.toml",
        help="設定ファイルのパス",
    )
    parser.add_argument(
        "--pcb-file",
        "-p",
        type=Path,
        required=True,
        help="KiCADファイル (.kicad_pcb) のパス",
    )
    parser.add_argument(
        "--tolerance",
        "-t",
        type=float,
        default=0.1,
        help="位置合わせの許容誤差 (mm)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="HeightMap保存先のJSONファイルパス (省略時: data/height_mesh/<config名>/<pcb名>_<時刻>.json)",
    )
    args = parser.parse_args()

    # デフォルト出力先の生成
    if args.output is None:
        config_name = Path(args.config).parent.name
        pcb_stem = Path(args.pcb_file).stem
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = PROJECT_ROOT / "data" / "height_mesh" / config_name
        output_dir.mkdir(parents=True, exist_ok=True)
        args.output = output_dir / f"{pcb_stem}_{timestamp}.json"

    result = setup_board_calibration(
        config_path=args.config,
        pcb_file_path=args.pcb_file,
        tolerance=args.tolerance,
        window_name=WINDOW_NAME,
    )

    klipper = result.klipper
    stage = result.stage
    machine = result.machine  # noqa: F841
    outline = result.pcb.outline

    probe = ProbeSensor(klipper.readonly)
    probe_executor = ProbeExecutor(klipper=klipper, probe=probe, stage=stage)

    with machine_session(klipper):
        board_transform = result.board_transform

        # Bed mesh計測
        print("\n=== Bed mesh計測 ===")
        # TODO: PasteDispenser再実装後に更新する
        # toolhead_offset = machine.paste_dispenser.toolhead.to_transform()
        height_measurer = HeightTransformMeasurer(
            probe_executor=probe_executor,
            klipper=klipper,
            stage=stage,
        )
        # TODO: PasteDispenser再実装後に toolhead_offset を復活させる
        height_map = height_measurer.measure(
            outline=outline,
            board_to_machine=board_transform,
        )

        # 保存
        height_map.save(args.output)
        print(f"\nHeightMapを保存しました: {args.output}")


if __name__ == "__main__":
    main()
