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
from shapely import Polygon

from pcbasm import gcode
from pcbasm.config import Machine, PadAlign, get_machine_config
from pcbasm.geometry import Compose, Point2d, sort_by_nearest
from pcbasm.hal import Speed
from pcbasm.pcb import Pad
from pcbasm.posctrl import (
    BoardCalibrationResult,
    ComponentPads,
    CopperProjector,
    PadAlignmentResult,
    PadAlignmentSession,
    PadResultRenderer,
    display_at_point,
    machine_session,
    setup_board_calibration,
    sorted_top_component_pads,
    wait_for_keypress,
    window_sink,
)
from pcbasm.utils import setup_logging
from pcbasm.vision import CopperEdgeDetector

WINDOW_NAME = "Board Tour Demo"
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

    合成は PadResultRenderer に委譲し、現在位置で固定した投影を
    一定時間ウィンドウへ流す。

    Returns:
        Escキーで中断された場合True
    """
    renderer = PadResultRenderer(
        projector=projector,
        edge_detector=edge_detector,
        roi_polygons=roi_polygons,
        paste_polygons=paste_polygons,
        pad_align=pad_align,
        position=result.stage.get_position().to2d(),
    )

    deadline = time.monotonic() + RESULT_DISPLAY_SEC
    while time.monotonic() < deadline:
        display = renderer.render(result.camera.capture(), lines)
        cv2.imshow(WINDOW_NAME, display.numpy())
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

    session = PadAlignmentSession.from_calibration(
        result, frame_sink=window_sink(WINDOW_NAME)
    )

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
    try:
        _run_tour(machine, args)
    finally:
        cv2.destroyAllWindows()


def _run_tour(machine: Machine, args: argparse.Namespace) -> None:
    """セットアップ〜四隅・パッド巡回の本体."""
    result = setup_board_calibration(
        machine=machine,
        pcb_file_path=args.pcb_file,
        tolerance=args.tolerance,
        frame_sink=window_sink(WINDOW_NAME),
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
