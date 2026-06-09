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
from shapely.geometry import (
    MultiPoint,
    Point as ShapelyPoint,
    Polygon as ShapelyPolygon,
)

from pcbasm.config import get_machine_config
from pcbasm.geometry import (
    HeightPlane,
    Point2d,
    Point3d,
    sample_points_in_polygons,
)
from pcbasm.pcb import Copper, Layer, PcbFile
from pcbasm.posctrl import setup_board_calibration
from pcbasm.session import PasteSession
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
    minx, miny, maxx, maxy = pcb.outline.polygon.bounds
    grid_x = np.linspace(minx, maxx, _MESH_RESOLUTION)
    grid_y = np.linspace(miny, maxy, _MESH_RESOLUTION)
    mesh_z = np.array(
        [
            [height_plane.apply(Point3d(float(x), float(y), 0.0)).z for x in grid_x]
            for y in grid_y
        ]
    )

    fig, ax = plt.subplots(figsize=(10, 8))
    heatmap = ax.imshow(
        mesh_z,
        extent=(minx, maxx, miny, maxy),
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
    _draw_planned_hull(ax, planned_points)
    ax.scatter(
        [p.x for p in planned_points],
        [p.y for p in planned_points],
        c="red",
        marker="x",
        s=80,
        label="Planned probe points",
    )
    for idx, p in enumerate(planned_points):
        ax.annotate(
            str(idx),
            (p.x, p.y),
            textcoords="offset points",
            xytext=(5, 5),
            fontsize=8,
            color="darkred",
        )
    ax.set_title(title)
    ax.legend(loc="upper right")
    plt.tight_layout()
    fig.savefig(output_path, dpi=120)
    plt.close(fig)


def _draw_planned_hull(ax: Axes, planned_points: list[Point2d]) -> None:
    """計測予定点の凸包を preview に薄線で描く."""
    if len(planned_points) < 3:
        return
    hull = MultiPoint([(p.x, p.y) for p in planned_points]).convex_hull
    if not isinstance(hull, ShapelyPolygon):
        return
    xs, ys = hull.exterior.xy
    ax.plot(xs, ys, color="red", alpha=0.35, linewidth=1.0, label="Probe hull")


def _print_sampling_diagnostics(
    planned_points: list[Point2d],
    top_coppers: list[Copper],
    pcb: PcbFile,
) -> None:
    """Planned points の安全余裕と基板カバレッジを標準出力へ出す."""
    if not planned_points:
        return

    clearances = [_clearance_to_copper(p, top_coppers) for p in planned_points]
    hull = MultiPoint([(p.x, p.y) for p in planned_points]).convex_hull
    outline_area = pcb.outline.polygon.area
    hull_ratio = hull.area / outline_area if outline_area > 0.0 else 0.0
    print(
        "Sampling diagnostics: "
        f"points={len(planned_points)}, "
        f"min_clearance={min(clearances):.3f}mm, "
        f"hull_area/outline_area={hull_ratio:.3f}"
    )


def _clearance_to_copper(point: Point2d, top_coppers: list[Copper]) -> float:
    """点が乗っている銅箔境界までの最小距離を返す."""
    shapely_point = ShapelyPoint(point.x, point.y)
    containing = [c.polygon for c in top_coppers if c.polygon.covers(shapely_point)]
    polygons = containing or [c.polygon for c in top_coppers]
    return min(p.boundary.distance(shapely_point) for p in polygons)


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(
        description="基板表面の高さ計測（2次曲面フィット）と可視化"
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
        outline=pcb.outline.polygon,
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
    _print_sampling_diagnostics(planned_points, top_coppers, pcb)
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
    session = PasteSession.from_calibration(result)

    with session:
        print("\n=== Height plane計測 ===")
        height_plane = session.height_measurer.measure(
            coppers=top_coppers,
            board_to_machine=session.board_to_machine,
            outline=pcb.outline.polygon,
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
