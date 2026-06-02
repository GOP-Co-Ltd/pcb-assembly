#!/usr/bin/env python3
"""基板表面の高さをHeightPlaneMeasurerで計測し、2DヒートマップPNGとして保存する."""

import argparse
import logging
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.axes import Axes
from matplotlib.patches import Polygon as MplPolygon

from pcbasm.config import get_machine_config
from pcbasm.geometry import (
    Compose,
    HeightPlane,
    Point2d,
    Point3d,
    sample_points_in_polygons,
)
from pcbasm.hal import ServoGroundProbe
from pcbasm.pasting import HeightPlaneMeasurer, ProbeExecutor
from pcbasm.pcb import Layer, PcbFile
from pcbasm.posctrl import machine_session, setup_board_calibration
from pcbasm.utils import PROJECT_ROOT, setup_logging
from pcbasm.visualization import polygon_with_holes_patch

WINDOW_NAME = "Height Plane"
_MESH_RESOLUTION = 50


def _draw_pcb_background(ax: Axes, pcb: PcbFile) -> None:
    """銅箔TOP層・基板アウトライン・軸範囲/ラベルを ax に描画する."""
    for cu in pcb.copper:
        if cu.layer != Layer.TOP:
            continue
        ax.add_patch(
            polygon_with_holes_patch(
                cu.polygon,
                facecolor="#cc8844",
                edgecolor="#cc8844",
                alpha=0.3,
                linewidth=0.3,
            )
        )

    outline_coords = list(pcb.outline.polygon.exterior.coords)
    ax.add_patch(
        MplPolygon(
            outline_coords,
            closed=True,
            facecolor="none",
            edgecolor="#ffffff",
            linewidth=1.5,
            linestyle="--",
        )
    )

    minx, miny, maxx, maxy = pcb.outline.polygon.bounds
    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)
    ax.set_aspect("equal")
    ax.set_xlabel("X [mm]")
    ax.set_ylabel("Y [mm]")


def _visualize(
    height_plane: HeightPlane,
    pcb: PcbFile,
    title: str,
    output_path: Path,
) -> None:
    """HeightPlaneの高さを2Dヒートマップ・基板アウトライン・銅箔と重ねてPNG保存する."""
    xs = [p.x for p in height_plane.points]
    ys = [p.y for p in height_plane.points]
    zs = [p.z for p in height_plane.points]

    # z=0で入力するとapply後のz値が補間値そのものになる
    grid_x = np.linspace(min(xs), max(xs), _MESH_RESOLUTION)
    grid_y = np.linspace(min(ys), max(ys), _MESH_RESOLUTION)
    mesh_z = np.array(
        [
            [height_plane.apply(Point3d(float(x), float(y), 0.0)).z for x in grid_x]
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

    _draw_pcb_background(ax, pcb)

    ax.scatter(
        xs, ys, c=zs, cmap="viridis", edgecolor="white", s=60, label="Probe points"
    )

    ax.set_title(title)
    ax.legend(loc="upper right")
    fig.colorbar(heatmap, ax=ax, label="Z [mm]")
    plt.tight_layout()
    fig.savefig(output_path, dpi=120)
    plt.close(fig)


def _visualize_planned(
    planned_points: list[Point2d],
    pcb: PcbFile,
    title: str,
    output_path: Path,
) -> None:
    """計測予定の probe 点を基板背景に重ねてPNG保存する."""
    fig, ax = plt.subplots(figsize=(10, 8))
    _draw_pcb_background(ax, pcb)
    ax.scatter(
        [p.x for p in planned_points],
        [p.y for p in planned_points],
        c="red",
        marker="x",
        s=80,
        label="Planned probe points",
    )
    ax.set_title(title)
    ax.legend(loc="upper right")
    plt.tight_layout()
    fig.savefig(output_path, dpi=120)
    plt.close(fig)


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(
        description="基板表面の高さ計測（平面フィット）と可視化"
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
        help="可視化PNGの保存先 (省略時: data/height_plane/<config名>/<pcb名>_<時刻>.png)",
    )
    args = parser.parse_args()

    pcb_stem = Path(args.pcb_file).stem
    if args.output is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = PROJECT_ROOT / "data" / "height_plane" / args.machine
        output_dir.mkdir(parents=True, exist_ok=True)
        args.output = output_dir / f"{pcb_stem}_{timestamp}.png"

    machine = get_machine_config(args.machine)
    probe_config = machine.probe

    pcb = PcbFile(args.pcb_file)
    top_coppers = [c for c in pcb.copper if c.layer == Layer.TOP]

    planned_points = sample_points_in_polygons(
        (c.polygon for c in top_coppers),
        min_radius=probe_config.min_radius,
        min_samples=probe_config.min_samples,
        max_samples=probe_config.max_samples,
    )

    preview_path = args.output.with_name(args.output.stem + "_preview.png")
    _visualize_planned(
        planned_points,
        pcb=pcb,
        title=f"Planned probe points: {pcb_stem} ({len(planned_points)} points)",
        output_path=preview_path,
    )
    print(f"\n計測予定ポイントを可視化しました: {preview_path}")
    print(f"計測点数: {len(planned_points)}")
    answer = input("これらの点を計測します。続行しますか？ [Y/n]: ").strip().lower()
    if answer not in ("", "y", "yes"):
        print("中止しました。")
        return

    result = setup_board_calibration(
        machine=machine,
        pcb_file_path=args.pcb_file,
        tolerance=args.tolerance,
        window_name=WINDOW_NAME,
    )

    klipper = result.klipper
    stage = result.stage
    machine = result.machine

    probe = ServoGroundProbe(
        klipper.readonly,
        servo_name=probe_config.servo_name,
        revolution_distance=probe_config.revolution_distance,
        down_distance=probe_config.down_distance,
    )
    probe_executor = ProbeExecutor(klipper=klipper, probe=probe, stage=stage)

    with machine_session(klipper):
        board_transform = result.board_transform

        print("\n=== Height plane計測 ===")
        toolhead_offset = machine.paste_dispenser.toolhead.to_transform()
        height_measurer = HeightPlaneMeasurer(
            probe_executor=probe_executor,
            klipper=klipper,
            stage=stage,
            min_radius=probe_config.min_radius,
            min_samples=probe_config.min_samples,
            max_samples=probe_config.max_samples,
        )
        height_plane = height_measurer.measure(
            coppers=top_coppers,
            board_to_machine=Compose([board_transform, toolhead_offset]),
        )

    _visualize(
        height_plane,
        pcb=pcb,
        title=f"Height Plane: {pcb_stem}",
        output_path=args.output,
    )
    print(f"\n可視化を保存しました: {args.output}")


if __name__ == "__main__":
    main()
