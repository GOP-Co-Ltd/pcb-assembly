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
9. 全パッドを巡回
"""

import argparse
import logging
from pathlib import Path

import cv2

from pcb_assembly import gcode
from pcb_assembly.control.setup import (
    BoardCalibrationResult,
    machine_session,
    setup_board_calibration,
)
from pcb_assembly.geometry import Move, Point2d, sort_by_nearest
from pcb_assembly.hal import Camera
from pcb_assembly.pcb import Layer
from pcb_assembly.utils import setup_logging
from pcb_assembly.vision import draw_overlay

PROJECT_ROOT = Path(__file__).parent.parent.parent
WINDOW_NAME = "Board Tour Demo"


def _display_at_point(
    result: BoardCalibrationResult,
    machine_pt: Point2d,
    label: str,
    duration: float = 0.5,
) -> None:
    """指定座標へ移動し、ラベル付きカメラ映像を一定時間表示する."""
    result.klipper.send_gcode(
        result.stage.to_gcode(Move(x=machine_pt.x, y=machine_pt.y, v=30))
        + gcode.wait_for_done()
    )

    crop_size = result.machine.camera.crop.size
    camera = result.camera
    for _ in range(int(camera.resolution.fps * duration)):
        frame = camera.capture()
        img = draw_overlay(frame, crop_size).numpy()
        cv2.putText(img, label, (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        cv2.imshow(WINDOW_NAME, img)
        if cv2.waitKey(1) == 27:  # Esc
            print("中断しました")
            break


def _wait_for_keypress(camera: Camera, crop_size: tuple[int, int]) -> None:
    """何かキーが押されるまでカメラ映像を表示し続ける."""
    print("\n何かキーを押すと終了します...")
    while True:
        frame = camera.capture()
        display = draw_overlay(frame, crop_size)
        cv2.imshow(WINDOW_NAME, display.numpy())
        if cv2.waitKey(100) != -1:
            break


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(description="ボード巡回デモ")
    parser.add_argument(
        "--config",
        "-c",
        type=Path,
        default=PROJECT_ROOT / "configs" / "pd_china_frame" / "machine.toml",
        help="設定ファイルのパス",
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

    result = setup_board_calibration(
        config_path=args.config,
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
            _display_at_point(result, machine_pt, f"Corner: {name}", duration=1.0)

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
                _display_at_point(result, machine_pt, label)

            print("コンポーネント巡回完了")
        else:
            print("巡回するコンポーネントがありません")

        # パッド巡回デモ
        print("\n=== パッド巡回デモ ===")
        top_pads = [p for p in pcb.pads if p.layer == Layer.TOP]
        print(f"TOPレイヤーのパッド数: {len(top_pads)}")

        if top_pads:
            # パッド中心をnearest neighborでソート
            current_pos = stage.get_position()
            pad_centers_3d = [p.center.to3d() for p in top_pads]
            sorted_centers = sort_by_nearest(pad_centers_3d, current_pos.to2d().to3d())

            print("巡回開始... (Escキーで中断)")
            for i, center_3d in enumerate(sorted_centers):
                machine_pt = board_transform.apply(center_3d.to2d())
                _display_at_point(
                    result, machine_pt, f"Pad {i + 1}/{len(sorted_centers)}"
                )

            print("巡回完了")
        else:
            print("巡回するパッドがありません")

        # ボード左上 (0, 0) に移動
        origin_machine = board_transform.apply(Point2d(0.0, 0.0))
        result.klipper.send_gcode(
            stage.to_gcode(Move(x=origin_machine.x, y=origin_machine.y, v=30))
            + gcode.wait_for_done()
        )

        _wait_for_keypress(result.camera, result.machine.camera.crop.size)


if __name__ == "__main__":
    main()
