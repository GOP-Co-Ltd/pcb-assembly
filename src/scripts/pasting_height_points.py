#!/usr/bin/env python3
"""基板表面の高さをプローブで計測し、HeightPointsを可視化するスクリプト.

処理順:
1. setup_board_calibration() で初期化〜Board変換計測
2. ProbeExecutor初期化
3. 高さ計測（HeightPointsMeasurer）
4. 補間した高さを2Dカラーヒートマップで描画しPNGに保存
"""

import argparse
import logging
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from pcb_assembly.config import get_machine_config
from pcb_assembly.control.adjust import HeightPointsMeasurer
from pcb_assembly.control.probe import ProbeExecutor
from pcb_assembly.control.setup import machine_session, setup_board_calibration
from pcb_assembly.geometry import Compose, HeightPoints, Point3d
from pcb_assembly.hal import Probe
from pcb_assembly.pcb import Layer
from pcb_assembly.utils import PROJECT_ROOT, setup_logging

WINDOW_NAME = "Height Points"
_MESH_RESOLUTION = 50


def _visualize(height_points: HeightPoints, title: str, output_path: Path) -> None:
    """HeightPointsの高さを2DカラーヒートマップとしてPNGに保存する."""
    xs = [p.x for p in height_points.points]
    ys = [p.y for p in height_points.points]
    zs = [p.z for p in height_points.points]

    # z=0で入力するとapply後のz値が補間値そのものになる
    grid_x = np.linspace(min(xs), max(xs), _MESH_RESOLUTION)
    grid_y = np.linspace(min(ys), max(ys), _MESH_RESOLUTION)
    mesh_z = np.array(
        [
            [height_points.apply(Point3d(float(x), float(y), 0.0)).z for x in grid_x]
            for y in grid_y
        ]
    )

    fig, ax = plt.subplots(figsize=(10, 8))
    heatmap = ax.imshow(
        mesh_z,
        extent=(min(xs), max(xs), min(ys), max(ys)),
        origin="lower",
        cmap="viridis",
        aspect="equal",
    )
    ax.scatter(
        xs, ys, c=zs, cmap="viridis", edgecolor="white", s=60, label="Probe points"
    )
    ax.set_xlabel("X [mm]")
    ax.set_ylabel("Y [mm]")
    ax.set_title(title)
    ax.legend(loc="upper right")
    fig.colorbar(heatmap, ax=ax, label="Z [mm]")
    plt.tight_layout()
    fig.savefig(output_path, dpi=120)
    plt.close(fig)


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(
        description="基板表面の高さ計測（散在点補間）と可視化"
    )
    parser.add_argument(
        "--machine",
        "-m",
        type=str,
        default="pd_china_frame",
        help="マシン名",
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
        help="可視化PNGの保存先 (省略時: data/height_points/<config名>/<pcb名>_<時刻>.png)",
    )
    args = parser.parse_args()

    if args.output is None:
        pcb_stem = Path(args.pcb_file).stem
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = PROJECT_ROOT / "data" / "height_points" / args.machine
        output_dir.mkdir(parents=True, exist_ok=True)
        args.output = output_dir / f"{pcb_stem}_{timestamp}.png"

    machine = get_machine_config(args.machine)
    result = setup_board_calibration(
        machine=machine,
        pcb_file_path=args.pcb_file,
        tolerance=args.tolerance,
        window_name=WINDOW_NAME,
    )

    klipper = result.klipper
    stage = result.stage
    machine = result.machine
    top_coppers = [c for c in result.pcb.copper if c.layer == Layer.TOP]

    probe_config = machine.probe
    probe = Probe(
        klipper.readonly,
        servo_name=probe_config.servo_name,
        revolution_distance=probe_config.revolution_distance,
        down_distance=probe_config.down_distance,
    )
    probe_executor = ProbeExecutor(klipper=klipper, probe=probe, stage=stage)

    with machine_session(klipper):
        board_transform = result.board_transform

        print("\n=== Height points計測 ===")
        toolhead_offset = machine.paste_dispenser.toolhead.to_transform()
        height_measurer = HeightPointsMeasurer(
            probe_executor=probe_executor,
            klipper=klipper,
            stage=stage,
            min_radius=probe_config.min_radius,
            min_samples=probe_config.min_samples,
            max_samples=probe_config.max_samples,
        )
        height_points = height_measurer.measure(
            coppers=top_coppers,
            board_to_machine=Compose([board_transform, toolhead_offset]),
        )

    pcb_stem = Path(args.pcb_file).stem
    _visualize(
        height_points, title=f"Height Points: {pcb_stem}", output_path=args.output
    )
    print(f"\n可視化を保存しました: {args.output}")


if __name__ == "__main__":
    main()
