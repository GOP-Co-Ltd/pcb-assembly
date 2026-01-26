#!/usr/bin/env python3
"""Reference Pointにカメラ中心を合わせるキャリブレーションスクリプト.

処理順:
1. 設定読み込み・初期化
2. G28でホーミング
3. Reference Pointへ移動
4. 縦横別々に移動して2点法でカメラ回転角・軸変換を取得
5. Reference Point中心にカメラ中心を合わせる
"""

import argparse
import logging
import time
from pathlib import Path

import cv2

from pcb_assembly import gcode
from pcb_assembly.config import Machine
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
WINDOW_NAME = "Reference Point Alignment"


def draw_overlay(
    image: Image,
    crop_size: tuple[int, int],
    offset: Point2d | None = None,
) -> Image:
    """画像に十字線、関心領域、オフセット情報を描画する."""
    img = image.numpy().copy()
    h, w = img.shape[:2]
    cx, cy = w // 2, h // 2

    color = (0, 255, 0)  # 緑

    # 中心に十字線を描画
    cv2.line(img, (cx - 30, cy), (cx + 30, cy), color, 1)
    cv2.line(img, (cx, cy - 30), (cx, cy + 30), color, 1)

    # 関心領域（crop領域）を矩形で描画
    half_w, half_h = crop_size[0] // 2, crop_size[1] // 2
    x1, y1 = cx - half_w, cy - half_h
    x2, y2 = cx + half_w, cy + half_h
    cv2.rectangle(img, (x1, y1), (x2, y2), color, 1)

    # オフセット情報を表示
    if offset is not None:
        text = f"Offset: ({offset.x:.3f}, {offset.y:.3f}) mm"
        cv2.putText(img, text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
        dist_text = f"Distance: {offset.norm:.3f} mm"
        cv2.putText(img, dist_text, (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)

    return Image(img)


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(
        description="Reference Pointにカメラ中心を合わせる"
    )
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
        help="位置合わせの許容誤差 (mm)",
    )
    args = parser.parse_args()

    # 設定読み込み
    print("=== 設定読み込み ===")
    machine = Machine(args.config)
    print(f"設定ファイル: {args.config}")

    # Klipper接続
    print("\n=== Klipper接続 ===")
    klipper = Klipper(host=machine.klipper.host, port=machine.klipper.port)
    print(f"接続先: {machine.klipper.host}:{machine.klipper.port}")
    stage = XYZStage(klipper.readonly)

    # カメラ初期化
    print("\n=== カメラ初期化 ===")
    cam_config = machine.camera
    camera = Camera(
        device_id=cam_config.device_id,
        width=cam_config.width,
        height=cam_config.height,
        fps=cam_config.fps,
        format=cam_config.format,
    )
    print(f"カメラ: {camera.info.name}")
    print(f"解像度: {cam_config.width}x{cam_config.height}")

    # キャリブレーション結果読み込み
    print("\n=== キャリブレーション読み込み ===")
    calibration = CalibrationResult.load(cam_config.calibration_file)
    print(f"pixel/mm: {calibration.pixel_per_mm:.2f}")

    # 円検出器初期化
    ref_config = machine.reference_point
    detector = CircleDetector(
        pixel_per_mm=calibration.pixel_per_mm,
        target_diameter_mm=ref_config.target_diameter,
        crop_size=cam_config.crop.size,
        diameter_tolerance_mm=0.5,
    )

    # ホーミング
    print("\n=== ホーミング (G28) ===")
    klipper.send_gcode(gcode.homing(x=True, y=True) + gcode.wait_for_done())
    print("ホーミング完了")

    # Reference Pointへ移動
    print("\n=== Reference Pointへ移動 ===")
    print(f"目標位置: ({ref_config.x}, {ref_config.y})")
    klipper.send_gcode(
        gcode.move(x=ref_config.x, y=ref_config.y) + gcode.wait_for_done()
    )
    print("移動完了")
    time.sleep(1.0)
    current_pos = stage.get_position()
    print(f"現在位置: ({current_pos.x:.3f}, {current_pos.y:.3f})")

    # オフセット検出関数を定義
    sample_count = 30
    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_AUTOSIZE)

    def observe_offset() -> Point2d:
        result = detector.detect_with_statistics(
            camera.capture() for _ in range(sample_count)
        )
        if result is None:
            raise RuntimeError("検出に失敗しました")

        display = draw_overlay(camera.capture(), cam_config.crop.size, result.mean_mm)
        cv2.imshow(WINDOW_NAME, display.numpy())
        cv2.waitKey(1)

        return result.mean_mm

    # カメラ回転角の計測（2点法）
    print("\n=== カメラ回転角の計測 ===")
    move_distance = (
        safe_move_distance(cam_config.crop.size, margin=0.3) / calibration.pixel_per_mm
    )
    offset_transform_measurer = OffsetTransformMeasurer(move_distance=move_distance)
    transform = offset_transform_measurer.measure(observe_offset, klipper, stage)

    # 位置合わせ
    print("\n=== カメラ中心を基準点に合わせる ===")
    position_adjustor = XYPositionAdjustor(tolerance=args.tolerance)

    try:
        final_pos = position_adjustor.adjust(
            lambda: transform.apply(observe_offset()),
            klipper,
            stage,
        )
        print(f"\n位置合わせ完了: ({final_pos.x:.4f}, {final_pos.y:.4f})")

        # 完了後も映像を表示し続ける（何かキーを押すまで）
        print("何かキーを押すと終了します...")
        final_offset = transform.apply(observe_offset())
        while True:
            frame = camera.capture()
            display = draw_overlay(frame, cam_config.crop.size, final_offset)
            cv2.imshow(WINDOW_NAME, display.numpy())
            if cv2.waitKey(100) != -1:
                break

    finally:
        klipper.send_gcode("M84")
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
