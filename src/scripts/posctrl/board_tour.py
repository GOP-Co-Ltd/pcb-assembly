#!/usr/bin/env python3
"""PCB上の四隅・コンポーネント・パッドを巡回するデモスクリプト.

Reference Pointの位置調整、Board座標→機械座標変換の計測も兼ねる。

処理順:
1. 設定読み込み・初期化
2. PCBファイルからOutlineを読み込み
3. G28でホーミング
4. Reference Point (top left) へ移動・位置調整
5. カメラ回転角の計測（OffsetTransformMeasurer）
6. Board変換の計測（BoardTransformMeasurer）
7. ボード四隅を巡回
8. 全コンポーネントを巡回
9. 全パッドを巡回し、銅箔照合で自動位置合わせ
"""

import argparse
import logging
from pathlib import Path

import cv2
import numpy as np

from pcbasm import gcode
from pcbasm.config import get_machine_config
from pcbasm.geometry import Point2d, sort_by_nearest
from pcbasm.hal import Speed
from pcbasm.pcb import Layer, Pad
from pcbasm.posctrl import (
    BoardCalibrationResult,
    CopperEdgeMatcher,
    CopperProjector,
    PadAligner,
    PadAlignmentResult,
    display_at_point,
    machine_session,
    setup_board_calibration,
    wait_for_keypress,
)
from pcbasm.utils import setup_logging
from pcbasm.vision import CopperEdgeDetector

WINDOW_NAME = "Board Tour Demo"
FILL_COLOR = (0, 0, 255)  # 想定銅箔overlayの色 (BGR)
FILL_ALPHA = 0.35


def _wait_with_overlay(
    result: BoardCalibrationResult,
    projector: CopperProjector,
    lines: list[str],
) -> bool:
    """現在位置で再投影したfill overlayとステータスを表示してキーを待つ.

    Returns:
        Escキーで中断された場合True、他キーならFalse
    """
    projection = projector.project(result.stage.get_position().to2d())
    fill = projection.fill_mask > 0
    while True:
        display = result.camera.capture().numpy().copy()
        color_layer = np.zeros_like(display)
        color_layer[:] = FILL_COLOR
        blended = cv2.addWeighted(
            display, 1.0 - FILL_ALPHA, color_layer, FILL_ALPHA, 0.0
        )
        display[fill] = blended[fill]

        for i, line in enumerate(lines):
            position = (10, 25 + i * 25)
            cv2.putText(
                display, line, position, cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3
            )
            cv2.putText(
                display,
                line,
                position,
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (255, 255, 255),
                1,
            )

        cv2.imshow(WINDOW_NAME, display)
        key = cv2.waitKey(100)
        if key == 27:  # Esc
            return True
        if key != -1:
            return False


def _tour_pads(result: BoardCalibrationResult, args: argparse.Namespace) -> None:
    """全padを巡回し、銅箔照合で自動位置合わせして結果を表示する."""
    stage = result.stage
    pcb = result.pcb
    board_transform = result.board_transform

    top_pads = [p for p in pcb.pads if p.layer == Layer.TOP]
    print(f"TOPレイヤーのパッド数: {len(top_pads)}")
    if not top_pads:
        print("巡回するパッドがありません")
        return

    polygons = [c.polygon for c in pcb.copper if c.layer == Layer.TOP]
    frame = result.camera.capture()
    projector = CopperProjector(
        polygons=polygons,
        board_transform=board_transform,
        offset_transform=result.offset_transform,
        pixel_per_mm=result.calibration.pixel_per_mm,
        image_size=frame.size,
    )
    matcher = CopperEdgeMatcher(
        pixel_per_mm=result.calibration.pixel_per_mm,
        search_window_mm=args.search_window,
        theta_range_degrees=args.theta_range,
    )
    edge_detector = CopperEdgeDetector(
        canny_low=args.canny_low,
        canny_high=args.canny_high,
        blur_ksize=args.blur_ksize,
    )
    aligner = PadAligner(
        camera=result.camera,
        klipper=result.klipper,
        stage=stage,
        projector=projector,
        matcher=matcher,
        edge_detector=edge_detector,
        board_transform=board_transform,
        offset_transform=result.offset_transform,
        roi_margin_mm=args.roi_margin,
        min_roi_mm=args.min_roi,
        tolerance=args.copper_tolerance,
    )

    # パッドをnearest neighborでソート
    current_pos = stage.get_position()
    pad_centers_3d = [p.center.to3d() for p in top_pads]
    sorted_centers = sort_by_nearest(pad_centers_3d, current_pos.to2d().to3d())
    center_to_pad = {p.center.to3d(): p for p in top_pads}
    sorted_pads = [center_to_pad[center] for center in sorted_centers]

    alignments: list[tuple[Pad, PadAlignmentResult]] = []
    print("巡回開始... (Escキーで中断)")
    for i, pad in enumerate(sorted_pads):
        label = f"{pad.designator}.{pad.pad_number} ({i + 1}/{len(sorted_pads)})"
        print(f"--- {label} ---")
        try:
            alignment = aligner.align(pad)
        except RuntimeError as exc:
            print(f"警告: {label} の位置合わせに失敗: {exc}")
            if _wait_with_overlay(result, projector, [label, f"FAILED: {exc}"]):
                print("中断しました")
                break
            continue

        alignments.append((pad, alignment))
        translation = alignment.translation
        lines = [
            label,
            f"dx,dy: ({translation.x:+.3f}, {translation.y:+.3f}) mm",
            f"theta: {alignment.rotation.degrees:+.3f} deg",
            f"mean distance: {alignment.match.mean_distance_px:.2f} px",
        ]
        if _wait_with_overlay(result, projector, lines):
            print("中断しました")
            break

    # サマリ表示
    print("\n=== 位置合わせサマリ ===")
    for pad, alignment in alignments:
        translation = alignment.translation
        print(
            f"{pad.designator}.{pad.pad_number}: "
            f"dx={translation.x:+.4f} dy={translation.y:+.4f} mm, "
            f"theta={alignment.rotation.degrees:+.3f} deg, "
            f"mean_distance={alignment.match.mean_distance_px:.2f} px"
        )
    print(f"成功: {len(alignments)}/{len(sorted_pads)}")


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(description="ボード巡回デモ")
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
        "--copper-tolerance",
        type=float,
        default=0.05,
        help="pad銅箔照合の収束許容誤差 (mm)",
    )
    parser.add_argument(
        "--search-window", type=float, default=2.0, help="照合の探索窓 片側幅 (mm)"
    )
    parser.add_argument(
        "--roi-margin", type=float, default=1.0, help="pad ROIのマージン (mm)"
    )
    parser.add_argument(
        "--min-roi", type=float, default=3.0, help="pad ROIの最小辺長 (mm)"
    )
    parser.add_argument(
        "--theta-range", type=float, default=2.0, help="θ探索の片側範囲 (度)"
    )
    parser.add_argument("--canny-low", type=float, default=100.0, help="Canny下側閾値")
    parser.add_argument("--canny-high", type=float, default=200.0, help="Canny上側閾値")
    parser.add_argument(
        "--blur-ksize", type=int, default=5, help="GaussianBlurカーネルサイズ (奇数)"
    )
    args = parser.parse_args()

    machine = get_machine_config(args.machine)
    result = setup_board_calibration(
        machine=machine,
        pcb_file_path=args.pcb_file,
        tolerance=args.tolerance,
        window_name=WINDOW_NAME,
    )

    with machine_session(result.klipper):
        board_transform = result.board_transform
        stage = result.stage
        pcb = result.pcb
        outline = pcb.outline

        # ボード四隅巡回デモ
        print("\n=== ボード四隅巡回デモ ===")
        corners = [
            ("左上", Point2d(0.0, 0.0)),
            ("右上", Point2d(outline.width, 0.0)),
            ("右下", Point2d(outline.width, outline.height)),
            ("左下", Point2d(0.0, outline.height)),
            ("左上", Point2d(0.0, 0.0)),
        ]

        for name, board_pt in corners:
            machine_pt = board_transform.apply(board_pt)
            print(
                f"{name}: Board({board_pt.x:.1f}, {board_pt.y:.1f}) -> "
                f"Machine({machine_pt.x:.3f}, {machine_pt.y:.3f})"
            )
            display_at_point(
                result,
                machine_pt,
                f"Corner: {name}",
                duration=1.0,
                window_name=WINDOW_NAME,
            )

        print("四隅巡回完了")

        # コンポーネント巡回デモ
        print("\n=== コンポーネント巡回デモ ===")
        top_components = [c for c in pcb.components if c.layer == Layer.TOP]
        print(f"TOPレイヤーのコンポーネント数: {len(top_components)}")

        if top_components:
            # コンポーネント位置をnearest neighborでソート
            current_pos = stage.get_position()
            comp_positions_3d = [c.position.to3d() for c in top_components]
            sorted_positions = sort_by_nearest(
                comp_positions_3d, current_pos.to2d().to3d()
            )

            # ソート順にコンポーネントを並べ替え
            pos_to_comp = {c.position.to3d(): c for c in top_components}
            sorted_components = [pos_to_comp[pos] for pos in sorted_positions]

            print("巡回開始... (Escキーで中断)")
            for i, comp in enumerate(sorted_components):
                machine_pt = board_transform.apply(comp.position)
                label = f"{comp.designator} ({i + 1}/{len(sorted_components)})"
                display_at_point(result, machine_pt, label, window_name=WINDOW_NAME)

            print("コンポーネント巡回完了")
        else:
            print("巡回するコンポーネントがありません")

        # パッド巡回デモ（銅箔照合による自動位置合わせ）
        print("\n=== パッド巡回デモ ===")
        _tour_pads(result, args)

        # ボード左上 (0, 0) に移動
        origin_machine = board_transform.apply(Point2d(0.0, 0.0))
        result.klipper.send_gcode(
            stage.move(x=origin_machine.x, y=origin_machine.y, speed=Speed.absolute(30))
            + gcode.wait_for_done()
        )

        wait_for_keypress(
            result.camera, result.machine.camera.crop.size, window_name=WINDOW_NAME
        )


if __name__ == "__main__":
    main()
