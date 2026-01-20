#!/usr/bin/env python3
"""チェッカーボードを使ったカメラキャリブレーションスクリプト."""

import argparse
from datetime import datetime
from pathlib import Path

import cv2

from pcb_assembly.hal.camera import Camera
from pcb_assembly.vision.calibration import CheckerboardCalibrator

PROJECT_ROOT = Path(__file__).parent.parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description="チェッカーボードキャリブレーション")
    parser.add_argument("--device", "-d", type=int, default=0, help="カメラデバイスID")
    parser.add_argument("--width", "-W", type=int, default=640, help="幅")
    parser.add_argument("--height", "-H", type=int, default=480, help="高さ")
    parser.add_argument(
        "--square-size",
        "-s",
        type=float,
        required=True,
        help="チェッカーボードの1マスのサイズ (mm)",
    )
    parser.add_argument("--crop-width", type=int, default=400, help="クロップ幅")
    parser.add_argument("--crop-height", type=int, default=400, help="クロップ高さ")
    args = parser.parse_args()

    output_dir = PROJECT_ROOT / "data" / "camera_calibration"
    output_dir.mkdir(parents=True, exist_ok=True)

    camera = Camera(
        device_id=args.device,
        width=args.width,
        height=args.height,
    )

    calibrator = CheckerboardCalibrator(
        square_size_mm=args.square_size,
        crop_size=(args.crop_width, args.crop_height),
    )

    print(f"カメラ: {camera.info.name}")
    print(f"解像度: {args.width}x{args.height}")
    print(f"クロップ: {args.crop_width}x{args.crop_height}")
    print(f"マスサイズ: {args.square_size}mm")
    print()
    print("操作方法:")
    print("  Space: 撮影してキャリブレーション")
    print("  q: 終了")

    while True:
        image = camera.capture()

        # クロップ領域を緑枠で表示
        h, w = image.height, image.width
        cx, cy = w // 2, h // 2
        half_w, half_h = args.crop_width // 2, args.crop_height // 2
        preview = image.numpy()
        cv2.rectangle(
            preview,
            (cx - half_w, cy - half_h),
            (cx + half_w, cy + half_h),
            (0, 255, 0),
            2,
        )
        cv2.imshow("Camera Calibration", preview)

        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            print("終了")
            break

        if key == ord(" "):
            print("キャリブレーション中...")
            detection = calibrator.calibrate(image)

            if detection is None:
                print("チェッカーボードが検出できませんでした")
                print("再度Spaceを押してください")
                continue

            result, vis = detection

            # 結果を保存
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            json_path = output_dir / f"{camera.info.name}_{timestamp}.json"
            image_path = output_dir / f"{camera.info.name}_{timestamp}.png"

            result.save(json_path)
            cv2.imwrite(str(image_path), vis.numpy())

            print()
            print("=== キャリブレーション完了 ===")
            print(f"pixel/mm: {result.pixel_per_mm:.2f}")
            print(f"mm/pixel: {result.mm_per_pixel:.4f}")
            print(f"標準偏差: {result.std_distance_px:.2f} px")
            print(f"JSON: {json_path}")
            print(f"画像: {image_path}")
            break

    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
