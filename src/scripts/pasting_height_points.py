#!/usr/bin/env python3
"""基板表面の高さをプローブで計測し、HeightPointsを可視化するスクリプト.

処理順:
1. setup_board_calibration() で初期化〜Board変換計測
2. ProbeExecutor初期化
3. 高さ計測（HeightPointsMeasurer）
4. matplotlibで計測点と補間サーフェスを3D可視化
"""

import argparse
import logging
from pathlib import Path
from typing import cast

import matplotlib.pyplot as plt
import numpy as np
from mpl_toolkits.mplot3d import Axes3D

from pcb_assembly.config import get_machine_config
from pcb_assembly.control.adjust import HeightPointsMeasurer
from pcb_assembly.control.probe import ProbeExecutor
from pcb_assembly.control.setup import machine_session, setup_board_calibration
from pcb_assembly.geometry import Compose, HeightPoints, Point3d
from pcb_assembly.hal import Probe
from pcb_assembly.utils import setup_logging

WINDOW_NAME = "Height Points"
_MESH_RESOLUTION = 50


def _visualize(height_points: HeightPoints, title: str) -> None:
    """HeightPointsの計測点と補間サーフェスを3Dプロットする."""
    xs = [p.x for p in height_points.points]
    ys = [p.y for p in height_points.points]
    zs = [p.z for p in height_points.points]

    # z=0で入力するとapply後のz値が補間値そのものになる
    grid_x = np.linspace(min(xs), max(xs), _MESH_RESOLUTION)
    grid_y = np.linspace(min(ys), max(ys), _MESH_RESOLUTION)
    mesh_x, mesh_y = np.meshgrid(grid_x, grid_y)
    mesh_z = np.array(
        [
            [height_points.apply(Point3d(float(x), float(y), 0.0)).z for x in grid_x]
            for y in grid_y
        ]
    )

    fig = plt.figure(figsize=(10, 8))
    ax = cast(Axes3D, fig.add_subplot(111, projection="3d"))
    ax.plot_surface(
        mesh_x,
        mesh_y,
        mesh_z,
        cmap="viridis",
        alpha=0.7,
        edgecolor="none",
    )
    ax.scatter(xs, ys, zs=zs, color="red", s=40, label="計測点")  # pyright: ignore[reportArgumentType]
    ax.set_xlabel("X [mm]")
    ax.set_ylabel("Y [mm]")
    ax.set_zlabel("Z [mm]")
    ax.set_title(title)
    ax.legend()
    plt.tight_layout()
    plt.show()


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
    args = parser.parse_args()

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
    outline = result.pcb.outline

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
        )
        height_points = height_measurer.measure(
            outline=outline,
            board_to_machine=Compose([board_transform, toolhead_offset]),
        )

    pcb_stem = Path(args.pcb_file).stem
    _visualize(height_points, title=f"Height Points: {pcb_stem}")


if __name__ == "__main__":
    main()
