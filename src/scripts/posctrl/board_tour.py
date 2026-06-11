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
import time
from pathlib import Path

import cv2
import numpy as np

from pcbasm import gcode
from pcbasm.config import PadAlign, get_machine_config
from pcbasm.geometry import Point2d, sort_by_nearest
from pcbasm.hal import Speed
from pcbasm.pcb import Layer, Pad
from pcbasm.posctrl import (
    BoardCalibrationResult,
    CopperEdgeMatcher,
    CopperProjector,
    PadAligner,
    PadAlignmentResult,
    PadGroup,
    display_at_point,
    group_pads,
    machine_session,
    setup_board_calibration,
    wait_for_keypress,
)
from pcbasm.utils import setup_logging
from pcbasm.vision import CopperEdgeDetector

WINDOW_NAME = "Board Tour Demo"
FILL_COLOR = (0, 0, 255)  # 対象pad overlayの色 (BGR)
FILL_ALPHA = 0.35
EXPECTED_COLOR = (0, 0, 255)  # 想定しているpadの輪郭 (BGR: 赤)
DETECTED_COLOR = (0, 255, 0)  # 検出された銅箔輪郭 (BGR: 緑)
RESULT_DISPLAY_SEC = 1.0  # 各padの結果表示時間（キー待ちはしない）


def _show_pad_result(
    result: BoardCalibrationResult,
    projector: CopperProjector,
    edge_detector: CopperEdgeDetector,
    pad: Pad,
    pad_align: PadAlign,
    lines: list[str],
) -> bool:
    """対象padのROIに限定したoverlayを一定時間表示して自動で次へ進む.

    対象pad領域の薄塗り + 想定しているpadの輪郭（赤）+
    検出された銅箔輪郭（緑）を現在位置の再投影で描画する。

    Returns:
        Escキーで中断された場合True
    """
    current = result.stage.get_position().to2d()
    projection = projector.project(current)
    x0, y0, x1, y1 = projector.roi_of(
        pad.polygon,
        current,
        margin_mm=pad_align.roi_margin,
        min_size_mm=pad_align.min_roi,
    )
    roi = np.zeros(projection.fill_mask.shape, dtype=bool)
    roi[y0:y1, x0:x1] = True
    fill = (projection.fill_mask > 0) & roi
    expected = (projection.edge_mask > 0) & roi

    deadline = time.monotonic() + RESULT_DISPLAY_SEC
    while time.monotonic() < deadline:
        image = result.camera.capture()
        detected = (edge_detector.detect_edges(image) > 0) & roi

        display = image.numpy().copy()
        color_layer = np.zeros_like(display)
        color_layer[:] = FILL_COLOR
        blended = cv2.addWeighted(
            display, 1.0 - FILL_ALPHA, color_layer, FILL_ALPHA, 0.0
        )
        display[fill] = blended[fill]
        display[expected] = EXPECTED_COLOR
        display[detected] = DETECTED_COLOR

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
        if cv2.waitKey(50) == 27:  # Esc
            return True
    return False


def _tour_pads(result: BoardCalibrationResult) -> None:
    """全padを巡回し、銅箔照合で自動位置合わせして結果を表示する."""
    stage = result.stage
    pcb = result.pcb
    board_transform = result.board_transform
    pad_align = result.machine.paste_dispenser.pad_align

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
        search_window_mm=pad_align.search_window,
        theta_range_degrees=pad_align.theta_range,
    )
    edge_detector = CopperEdgeDetector(
        canny_low=pad_align.canny_low,
        canny_high=pad_align.canny_high,
        blur_ksize=pad_align.blur_ksize,
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
        roi_margin_mm=pad_align.roi_margin,
        min_roi_mm=pad_align.min_roi,
        tolerance=pad_align.tolerance,
        window_name=WINDOW_NAME,
    )

    # 区画ごとにグループ化し、代表padの位置でnearest neighborソート
    groups = group_pads(top_pads, pad_align.cell_size)
    print(f"区画数: {len(groups)} (区画サイズ {pad_align.cell_size} mm)")
    current_pos = stage.get_position()
    rep_centers_3d = [g.representative.center.to3d() for g in groups]
    sorted_centers = sort_by_nearest(rep_centers_3d, current_pos.to2d().to3d())
    center_to_group = {g.representative.center.to3d(): g for g in groups}
    sorted_groups = [center_to_group[center] for center in sorted_centers]

    alignments: list[tuple[PadGroup, Pad, PadAlignmentResult]] = []
    print("巡回開始... (Escキーで中断)")
    for i, group in enumerate(sorted_groups):
        progress = f"cell({group.cell[0]},{group.cell[1]}) {i + 1}/{len(sorted_groups)}"
        print(f"--- {progress}: {len(group.pads)} pads ---")

        # 重心に近い順に照合を試す（代表が失敗したら次のpadへフォールバック）
        aligned: tuple[Pad, PadAlignmentResult] | None = None
        for pad in group.pads:
            label = f"{pad.designator}.{pad.pad_number} {progress}"
            try:
                aligned = (pad, aligner.align(pad))
                break
            except RuntimeError as exc:
                print(f"警告: {label} の照合に失敗: {exc}")

        if aligned is None:
            print(f"警告: 区画 {group.cell} は全padで照合に失敗")
            if _show_pad_result(
                result,
                projector,
                edge_detector,
                group.representative,
                pad_align,
                [progress, "FAILED"],
            ):
                print("中断しました")
                break
            continue

        pad, alignment = aligned
        alignments.append((group, pad, alignment))
        translation = alignment.translation
        lines = [
            f"{pad.designator}.{pad.pad_number} {progress} ({len(group.pads)} pads)",
            f"dx,dy: ({translation.x:+.3f}, {translation.y:+.3f}) mm",
            f"theta: {alignment.rotation.degrees:+.3f} deg",
            f"mean distance: {alignment.match.mean_distance_px:.2f} px",
        ]
        if _show_pad_result(result, projector, edge_detector, pad, pad_align, lines):
            print("中断しました")
            break

    # サマリ表示（区画内の全padは代表のTransformを共有する）
    print("\n=== 位置合わせサマリ（区画ごと、区画内padは代表のTransformを共有） ===")
    for group, pad, alignment in alignments:
        translation = alignment.translation
        print(
            f"cell({group.cell[0]},{group.cell[1]}) "
            f"代表 {pad.designator}.{pad.pad_number} ({len(group.pads)} pads): "
            f"dx={translation.x:+.4f} dy={translation.y:+.4f} mm, "
            f"theta={alignment.rotation.degrees:+.3f} deg, "
            f"mean_distance={alignment.match.mean_distance_px:.2f} px"
        )
    aligned_pads = sum(len(group.pads) for group, _, _ in alignments)
    print(f"成功: {len(alignments)}/{len(sorted_groups)} 区画 ({aligned_pads} pads)")


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
        _tour_pads(result)

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
