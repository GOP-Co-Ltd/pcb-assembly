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
8. 部品ごとに銅箔照合で自動位置合わせし、補正適用済みの全パッドを巡回
"""

import argparse
import logging
import time
from collections.abc import Sequence
from pathlib import Path

import cv2
import numpy as np
from shapely import Polygon

from pcbasm import gcode
from pcbasm.config import PadAlign, get_machine_config
from pcbasm.geometry import Compose, Point2d, sort_by_nearest
from pcbasm.hal import Speed
from pcbasm.pcb import Pad
from pcbasm.posctrl import (
    BoardCalibrationResult,
    ComponentPads,
    CopperProjector,
    PadAlignmentResult,
    PadAlignmentSession,
    display_at_point,
    machine_session,
    setup_board_calibration,
    sorted_top_component_pads,
    wait_for_keypress,
)
from pcbasm.utils import setup_logging
from pcbasm.vision import CopperEdgeDetector, draw_crosshair

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
    roi_polygons: Sequence[Polygon],
    paste_polygons: Sequence[Polygon],
    pad_align: PadAlign,
    lines: list[str],
) -> bool:
    """対象領域のROIに限定したoverlayを一定時間表示して自動で次へ進む.

    塗布対象（ペーストpad領域）の薄塗り + 想定している銅箔の輪郭（赤）+
    検出された銅箔輪郭（緑）+ 中心十字を現在位置の再投影で描画する。
    ROIは実銅箔（roi_polygons）から決め、薄塗りはペースト開口
    （paste_polygons）を投影する。

    Returns:
        Escキーで中断された場合True
    """
    current = result.stage.get_position().to2d()
    projection = projector.project(current)
    x0, y0, x1, y1 = projector.roi_of(
        roi_polygons,
        current,
        margin_mm=pad_align.roi_margin,
        min_size_mm=pad_align.min_roi,
    )
    roi = np.zeros(projection.edge_mask.shape, dtype=bool)
    roi[y0:y1, x0:x1] = True
    expected = (projection.edge_mask > 0) & roi

    # ペーストpad領域（塗布対象）を投影して薄塗りマスクを作る
    fill_mask = np.zeros(projection.edge_mask.shape, dtype=np.uint8)
    for paste in paste_polygons:
        pixels = [
            projector.pixel_of(Point2d(float(x), float(y)), current)
            for x, y in paste.exterior.coords
        ]
        points = np.array([[round(p.x), round(p.y)] for p in pixels], dtype=np.int32)
        cv2.fillPoly(fill_mask, [points], 255)
    fill = fill_mask > 0

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
        draw_crosshair(display)

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
    pad_align = result.machine.paste_dispenser.pad_align

    # padをdesignatorで部品へ対応付け、部品座標でnearest neighborソート
    sorted_groups = sorted_top_component_pads(result)
    print(f"padを持つ部品数: {len(sorted_groups)}")
    if not sorted_groups:
        print("巡回するパッドがありません")
        return

    session = PadAlignmentSession.from_calibration(result, window_name=WINDOW_NAME)

    alignments: list[tuple[ComponentPads, PadAlignmentResult]] = []
    aborted = False
    print("巡回開始... (Escキーで中断)")
    for i, group in enumerate(sorted_groups):
        designator = group.component.designator
        progress = f"{designator} {i + 1}/{len(sorted_groups)}"
        print(f"--- {progress}: {len(group.pads)} pads ---")

        alignment = session.align(group)
        if alignment is None:
            print(f"警告: {progress} の照合に失敗")
            if _show_pad_result(
                result,
                session.projector,
                session.edge_detector,
                [p.copper_polygon for p in group.pads],
                [p.polygon for p in group.pads],
                pad_align,
                [progress, "FAILED"],
            ):
                aborted = True
                print("中断しました")
                break
            continue

        alignments.append((group, alignment))
        translation = alignment.translation
        print(
            f"  {designator}: "
            f"dx={translation.x:+.4f} dy={translation.y:+.4f} mm, "
            f"theta={alignment.rotation.degrees:+.3f} deg, "
            f"mean_distance={alignment.match.mean_distance_px:.2f} px"
        )
        if cv2.waitKey(1) == 27:  # Esc
            aborted = True
            print("中断しました")
            break

    # サマリ表示（部品の全padは部品のTransformを共有する）
    print("\n=== 位置合わせサマリ（部品内padは部品のTransformを共有） ===")
    for group, alignment in alignments:
        translation = alignment.translation
        print(
            f"{group.component.designator} ({len(group.pads)} pads): "
            f"dx={translation.x:+.4f} dy={translation.y:+.4f} mm, "
            f"theta={alignment.rotation.degrees:+.3f} deg, "
            f"mean_distance={alignment.match.mean_distance_px:.2f} px"
        )
    aligned_pads = sum(len(group.pads) for group, _ in alignments)
    print(f"成功: {len(alignments)}/{len(sorted_groups)} 部品 ({aligned_pads} pads)")

    if not aborted and alignments:
        _tour_corrected_pads(result, session, alignments)


def _tour_corrected_pads(
    result: BoardCalibrationResult,
    session: PadAlignmentSession,
    alignments: list[tuple[ComponentPads, PadAlignmentResult]],
) -> None:
    """所属部品の補正Transformを適用した位置で全padを巡回する.

    各padの目標位置は corrected =
    machine_transform(board_transform(pad.center))。
    overlayも補正済みのboard変換で投影するため、補正が正しければ 想定輪郭（赤）が実銅箔（緑）に重なって見える。
    """
    stage = result.stage
    pad_align = result.machine.paste_dispenser.pad_align

    # 部品ごとに補正済みのboard変換とprojectorを作り、padごとの巡回先を集める
    entries: list[tuple[Pad, CopperProjector, Point2d]] = []
    for group, alignment in alignments:
        corrected_transform = Compose(
            [result.board_transform, alignment.machine_transform]
        )
        corrected_projector = session.corrected_projector(alignment.machine_transform)
        for pad in group.pads:
            target = corrected_transform.apply(pad.center)
            entries.append((pad, corrected_projector, target))

    # 補正後の目標位置でnearest neighborソート
    current_pos = stage.get_position()
    sorted_entries = sort_by_nearest(
        entries, current_pos.to2d().to3d(), key=lambda entry: entry[2].to3d()
    )

    print("\n=== 補正適用済みの全pad巡回 === (Escキーで中断)")
    for i, (pad, corrected_projector, target) in enumerate(sorted_entries):
        result.klipper.send_gcode(
            stage.move(x=target.x, y=target.y, speed=Speed.rate(0.5))
            + gcode.wait_for_done()
        )
        lines = [
            f"{pad.designator}.{pad.pad_number} "
            f"corrected {i + 1}/{len(sorted_entries)}"
        ]
        if _show_pad_result(
            result,
            corrected_projector,
            session.edge_detector,
            [pad.copper_polygon],
            [pad.polygon],
            pad_align,
            lines,
        ):
            print("中断しました")
            break
    else:
        print(f"補正適用巡回完了: {len(sorted_entries)} pads")


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
