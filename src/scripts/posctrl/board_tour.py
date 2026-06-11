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
from collections.abc import Sequence
from pathlib import Path

import cv2
import numpy as np
from shapely import Polygon

from pcbasm import gcode
from pcbasm.config import PadAlign, get_machine_config
from pcbasm.geometry import Compose, Point2d, sort_by_nearest
from pcbasm.hal import Speed
from pcbasm.pcb import Layer, Pad
from pcbasm.posctrl import (
    BoardCalibrationResult,
    ComponentPads,
    CopperEdgeMatcher,
    CopperProjector,
    PadAligner,
    PadAlignmentResult,
    display_at_point,
    group_pads_by_component,
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
    polygons: Sequence[Polygon],
    pad_align: PadAlign,
    lines: list[str],
) -> bool:
    """対象領域のROIに限定したoverlayを一定時間表示して自動で次へ進む.

    対象領域の薄塗り + 想定している銅箔の輪郭（赤）+
    検出された銅箔輪郭（緑）を現在位置の再投影で描画する。

    Returns:
        Escキーで中断された場合True
    """
    current = result.stage.get_position().to2d()
    projection = projector.project(current)
    x0, y0, x1, y1 = projector.roi_of(
        polygons,
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
        max_correction_mm=pad_align.max_correction,
        window_name=WINDOW_NAME,
    )

    # padをdesignatorで部品へ対応付け、部品座標でnearest neighborソート
    top_components = [c for c in pcb.components if c.layer == Layer.TOP]
    groups = group_pads_by_component(top_components, top_pads)
    print(f"padを持つ部品数: {len(groups)}")
    current_pos = stage.get_position()
    positions_3d = [g.component.position.to3d() for g in groups]
    sorted_positions = sort_by_nearest(positions_3d, current_pos.to2d().to3d())
    position_to_group = {g.component.position.to3d(): g for g in groups}
    sorted_groups = [position_to_group[position] for position in sorted_positions]

    alignments: list[tuple[ComponentPads, PadAlignmentResult]] = []
    aborted = False
    print("巡回開始... (Escキーで中断)")
    for i, group in enumerate(sorted_groups):
        designator = group.component.designator
        progress = f"{designator} {i + 1}/{len(sorted_groups)}"
        print(f"--- {progress}: {len(group.pads)} pads ---")

        try:
            alignment = aligner.align(group)
        except RuntimeError as exc:
            print(f"警告: {progress} の照合に失敗: {exc}")
            if _show_pad_result(
                result,
                projector,
                edge_detector,
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
        _tour_corrected_pads(result, edge_detector, polygons, alignments)


def _tour_corrected_pads(
    result: BoardCalibrationResult,
    edge_detector: CopperEdgeDetector,
    polygons: Sequence[Polygon],
    alignments: list[tuple[ComponentPads, PadAlignmentResult]],
) -> None:
    """所属部品の補正Transformを適用した位置で全padを巡回する.

    各padの目標位置は corrected =
    machine_transform(board_transform(pad.center))。
    overlayも補正済みのboard変換で投影するため、補正が正しければ 想定輪郭（赤）が実銅箔（緑）に重なって見える。
    """
    stage = result.stage
    pad_align = result.machine.paste_dispenser.pad_align
    image_size = result.camera.capture().size

    # 部品ごとに補正済みのboard変換とprojectorを作り、padごとの巡回先を集める
    entries: list[tuple[Pad, CopperProjector, Point2d]] = []
    for group, alignment in alignments:
        corrected_transform = Compose(
            [result.board_transform, alignment.machine_transform]
        )
        corrected_projector = CopperProjector(
            polygons=polygons,
            board_transform=corrected_transform,
            offset_transform=result.offset_transform,
            pixel_per_mm=result.calibration.pixel_per_mm,
            image_size=image_size,
        )
        for pad in group.pads:
            target = corrected_transform.apply(pad.center)
            entries.append((pad, corrected_projector, target))

    # 補正後の目標位置でnearest neighborソート
    current_pos = stage.get_position()
    targets_3d = [target.to3d() for _, _, target in entries]
    sorted_targets = sort_by_nearest(targets_3d, current_pos.to2d().to3d())
    target_to_entry = {entry[2].to3d(): entry for entry in entries}
    sorted_entries = [target_to_entry[target] for target in sorted_targets]

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
            edge_detector,
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
