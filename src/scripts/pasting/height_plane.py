#!/usr/bin/env python3
"""基板表面の高さをHeightPlaneMeasurerで計測し、2DヒートマップPNGとして保存する."""

import argparse
import logging
from datetime import datetime
from pathlib import Path

from pcbasm.config import get_machine_config
from pcbasm.geometry import (
    Point2d,
    sample_points_in_polygons,
    sampling_diagnostics,
)
from pcbasm.pcb import Copper, Layer, PcbFile
from pcbasm.posctrl import setup_board_calibration, window_sink
from pcbasm.session import PasteSession
from pcbasm.utils import PROJECT_ROOT, setup_logging
from pcbasm.visualization import render_height_plane, render_planned_points

WINDOW_NAME = "Height Plane"


def _print_sampling_diagnostics(
    planned_points: list[Point2d],
    top_coppers: list[Copper],
    pcb: PcbFile,
) -> None:
    """Planned points の安全余裕と基板カバレッジを標準出力へ出す."""
    diagnostics = sampling_diagnostics(
        planned_points,
        [c.polygon for c in top_coppers],
        pcb.outline.polygon,
    )
    if diagnostics is None:
        return
    print(
        "Sampling diagnostics: "
        f"points={diagnostics.point_count}, "
        f"min_clearance={diagnostics.min_clearance:.3f}mm, "
        f"hull_area/outline_area={diagnostics.hull_area_ratio:.3f}"
    )


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
    render_planned_points(
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
        frame_sink=window_sink(WINDOW_NAME),
    )
    session = PasteSession.from_calibration(result)

    with session:
        print("\n=== Height plane計測 ===")
        height_plane = session.height_measurer.measure(
            coppers=top_coppers,
            board_to_machine=session.board_to_machine,
            outline=pcb.outline.polygon,
        )

    render_height_plane(
        height_plane,
        pcb=pcb,
        title=f"Height Plane: {pcb_stem}",
        output_path=args.output,
    )
    print(f"\n可視化を保存しました: {args.output}")


if __name__ == "__main__":
    main()
