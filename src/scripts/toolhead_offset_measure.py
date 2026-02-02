#!/usr/bin/env python3
"""Toolheadオフセットの手動計測スクリプト.

カメラで基準点に自動位置合わせした後、入力したオフセット分だけ移動し、
MoonrakerのWeb UIで微調整してToolheadのオフセット値を計測する。

手順:
1. 基準点に自動位置合わせ
2. オフセット初期値を入力して移動
3. ブラウザのMoonraker UIで微調整
4. Enterを押してオフセット値を確定
"""

import argparse
import logging
import threading
import time
from pathlib import Path

import cv2

from pcb_assembly import gcode
from pcb_assembly.config import Corner, Machine
from pcb_assembly.control.adjust import OffsetTransformMeasurer, XYPositionAdjustor
from pcb_assembly.geometry import Point2d
from pcb_assembly.hal import Camera, Klipper, XYZStage
from pcb_assembly.utils import setup_logging
from pcb_assembly.vision import (
    CalibrationResult,
    CircleDetector,
    Image,
    safe_move_distance,
)

PROJECT_ROOT = Path(__file__).parent.parent.parent
WINDOW_NAME = "Toolhead Offset Measure"


def draw_overlay(image: Image, crop_size: tuple[int, int]) -> Image:
    """画像に十字線と関心領域を描画する."""
    img = image.numpy().copy()
    h, w = img.shape[:2]
    cx, cy = w // 2, h // 2
    color = (0, 255, 0)
    cv2.line(img, (cx - 30, cy), (cx + 30, cy), color, 1)
    cv2.line(img, (cx, cy - 30), (cx, cy + 30), color, 1)
    half_w, half_h = crop_size[0] // 2, crop_size[1] // 2
    cv2.rectangle(img, (cx - half_w, cy - half_h), (cx + half_w, cy + half_h), color, 1)
    return Image(img)


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(description="Toolheadオフセット手動計測")
    parser.add_argument(
        "--config",
        "-c",
        type=Path,
        default=PROJECT_ROOT / "configs" / "pd_china_frame" / "machine.toml",
        help="設定ファイルのパス",
    )
    parser.add_argument(
        "--tolerance",
        "-t",
        type=float,
        default=0.01,
        help="自動位置合わせの許容誤差 (mm)",
    )
    args = parser.parse_args()

    # 設定読み込み
    print("=== 設定読み込み ===")
    machine = Machine(args.config)

    # Klipper接続
    klipper_config = machine.klipper
    klipper = Klipper(host=klipper_config.host, port=klipper_config.port)
    stage = XYZStage(klipper.readonly)

    # カメラ初期化
    cam_config = machine.camera
    camera = Camera(
        device_id=cam_config.device_id,
        width=cam_config.width,
        height=cam_config.height,
        fps=cam_config.fps,
        format=cam_config.format,
    )

    # キャリブレーション読み込み
    calibration = CalibrationResult.load(cam_config.calibration_file)

    # 円検出器
    ref_config = machine.reference_point
    detector = CircleDetector(
        pixel_per_mm=calibration.pixel_per_mm,
        target_diameter_mm=ref_config.target_diameter,
        crop_size=cam_config.crop.size,
        diameter_tolerance_mm=0.5,
    )

    # ホーミング
    print("\n=== ホーミング (G28) ===")
    klipper.send_gcode(gcode.homing() + gcode.wait_for_done())
    print("ホーミング完了")

    # Reference Pointへ移動
    print(f"\n=== Reference Point ({ref_config.x}, {ref_config.y}) へ移動 ===")
    klipper.send_gcode(
        gcode.move(x=ref_config.x, y=ref_config.y, velocity=20) + gcode.wait_for_done()
    )
    time.sleep(1.0)

    # オフセット検出関数
    sample_count = 30
    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_AUTOSIZE)

    def observe_offset() -> Point2d:
        result = detector.detect_with_statistics(
            camera.capture() for _ in range(sample_count)
        )
        if result is None:
            raise RuntimeError("基準点の検出に失敗しました")
        display = draw_overlay(camera.capture(), cam_config.crop.size)
        cv2.imshow(WINDOW_NAME, display.numpy())
        cv2.waitKey(1)
        return result.mean_mm

    # カメラ回転角計測
    print("\n=== カメラ回転角の計測 ===")
    move_distance = (
        safe_move_distance(cam_config.crop.size, margin=0.3) / calibration.pixel_per_mm
    )
    offset_transform_measurer = OffsetTransformMeasurer(move_distance=move_distance)
    offset_transform = offset_transform_measurer.measure(observe_offset, klipper, stage)

    def corrected_offset() -> Point2d:
        return offset_transform.apply(observe_offset())

    # 自動位置合わせ
    print("\n=== 基準点への自動位置合わせ ===")
    position_adjustor = XYPositionAdjustor(tolerance=args.tolerance)
    position_adjustor.adjust(corrected_offset, klipper, stage)
    print("位置合わせ完了")

    # ボード左上へ移動（基準点 - top_leftオフセット）
    ref_pos = stage.get_position().to2d()
    board_origin = ref_pos - ref_config.offsets.get(Corner.TOP_LEFT)
    print(f"\n=== ボード左上へ移動 ({board_origin.x:.3f}, {board_origin.y:.3f}) ===")
    klipper.send_gcode(
        gcode.move(x=board_origin.x, y=board_origin.y, velocity=20)
        + gcode.wait_for_done()
    )

    # オフセット量を対話入力して移動
    print("\n=== Toolheadオフセットの初期値を入力 ===")
    initial_offset = Point2d(
        x=float(input("X オフセット (mm): ")),
        y=float(input("Y オフセット (mm): ")),
    )
    target = board_origin + initial_offset
    print(f"\n移動先: ({target.x:.3f}, {target.y:.3f})")
    klipper.send_gcode(
        gcode.move(x=target.x, y=target.y, velocity=20) + gcode.wait_for_done()
    )

    # Moonraker UIで微調整（input()をバックグラウンドスレッドで待つ）
    print("\n=== 微調整 ===")
    print(f"Moonraker UI: http://{klipper_config.host}")
    print("ブラウザでToolheadの位置を微調整してください。")
    print("微調整が完了したらEnterを押してください...")

    enter_pressed = threading.Event()
    threading.Thread(target=lambda: (input(), enter_pressed.set()), daemon=True).start()

    # メインスレッドでカメラプレビューを表示（Enter待ち中）
    while not enter_pressed.is_set():
        display = draw_overlay(camera.capture(), cam_config.crop.size)
        cv2.imshow(WINDOW_NAME, display.numpy())
        cv2.waitKey(33)  # ~30fps

    cv2.destroyAllWindows()

    # 最終位置からオフセットを計算
    final_offset = stage.get_position().to2d() - board_origin

    # 結果表示
    print("\n=== 計測結果 ===")
    print(f"Toolhead オフセット: X={final_offset.x:.3f} Y={final_offset.y:.3f}")
    print()
    print("machine.tomlに設定する値:")
    print("[toolhead]")
    print(f"x = {final_offset.x}")
    print(f"y = {final_offset.y}")

    klipper.send_gcode(gcode.relax())


if __name__ == "__main__":
    main()
