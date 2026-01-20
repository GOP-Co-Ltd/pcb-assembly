#!/usr/bin/env python3
"""円検出デモスクリプト: 画像から円を検出し、中心からのズレを表示."""

import argparse
from pathlib import Path

import cv2

from pcb_assembly.hal.camera import Camera
from pcb_assembly.vision.calibration import CalibrationResult
from pcb_assembly.vision.detection import CircleDetector

PROJECT_ROOT = Path(__file__).parent.parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description="円検出デモ")
    parser.add_argument("--device", "-d", type=int, default=0, help="カメラデバイスID")
    parser.add_argument("--width", "-W", type=int, default=640, help="幅")
    parser.add_argument("--height", "-H", type=int, default=480, help="高さ")
    parser.add_argument(
        "--calibration",
        "-c",
        type=Path,
        required=True,
        help="キャリブレーションJSONファイル",
    )
    parser.add_argument(
        "--diameter",
        "-D",
        type=float,
        default=3.0,
        help="ターゲット円の直径 (mm)",
    )
    parser.add_argument(
        "--tolerance",
        "-t",
        type=float,
        default=1.0,
        help="直径の許容誤差 (mm)",
    )
    args = parser.parse_args()

    calibration = CalibrationResult.load(args.calibration)

    camera = Camera(
        device_id=args.device,
        width=args.width,
        height=args.height,
    )

    detector = CircleDetector(
        pixel_per_mm=calibration.pixel_per_mm,
        target_diameter_mm=args.diameter,
        diameter_tolerance_mm=args.tolerance,
        crop_size=calibration.crop_size,
    )

    print(f"カメラ: {camera.info.name}")
    print(f"解像度: {args.width}x{args.height}")
    print(f"pixel/mm: {calibration.pixel_per_mm:.2f}")
    print(f"ターゲット直径: {args.diameter}mm (±{args.tolerance}mm)")
    print()
    print("操作方法:")
    print("  q: 終了")

    while True:
        image = camera.capture()

        # クロップ領域を描画
        h, w = image.height, image.width
        cx, cy = w // 2, h // 2
        crop_w, crop_h = calibration.crop_size
        half_w, half_h = crop_w // 2, crop_h // 2

        preview = image.numpy()

        # クロップ領域の枠
        cv2.rectangle(
            preview,
            (cx - half_w, cy - half_h),
            (cx + half_w, cy + half_h),
            (0, 255, 0),
            1,
        )

        # 中心のクロスヘア
        cv2.line(preview, (cx - 20, cy), (cx + 20, cy), (0, 255, 0), 1)
        cv2.line(preview, (cx, cy - 20), (cx, cy + 20), (0, 255, 0), 1)

        # 円を検出
        result = detector.detect_nearest_center(image)

        if result is not None:
            # 検出した円を描画（クロップ領域の座標系から画像座標系に変換）
            circle_x = int(cx - half_w + result.center.x)
            circle_y = int(cy - half_h + result.center.y)
            radius = int(result.radius)

            # 円を描画
            cv2.circle(preview, (circle_x, circle_y), radius, (0, 0, 255), 2)
            cv2.circle(preview, (circle_x, circle_y), 3, (0, 0, 255), -1)

            # 中心からのオフセットを線で表示
            cv2.line(preview, (cx, cy), (circle_x, circle_y), (255, 0, 0), 2)

            # オフセット情報を表示
            text = f"X: {result.offset.mm.x:+.2f}mm  Y: {result.offset.mm.y:+.2f}mm"
            cv2.putText(
                preview, text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2
            )
            text = f"Distance: {result.offset.mm.distance:.2f}mm"
            cv2.putText(
                preview, text, (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2
            )
        else:
            cv2.putText(
                preview,
                "No circle detected",
                (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 0, 255),
                2,
            )

        cv2.imshow("Circle Detection Demo", preview)

        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            print("終了")
            break

    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
