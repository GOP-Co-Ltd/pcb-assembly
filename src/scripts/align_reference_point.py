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
import math
import time
from pathlib import Path

import cv2

from pcb_assembly import gcode
from pcb_assembly.config import Machine
from pcb_assembly.geometry import Position, Transform
from pcb_assembly.hal import Camera, Klipper, XYZStage
from pcb_assembly.vision import (
    CalibrationResult,
    CircleDetector,
    DetectedCircle,
    Image,
    Point2D,
)

PROJECT_ROOT = Path(__file__).parent.parent.parent
WINDOW_NAME = "Reference Point Alignment"


def draw_crosshair(image: Image, offset: Point2D | None = None) -> Image:
    """画像に十字線とオフセット情報を描画する."""
    img = image.numpy().copy()
    h, w = img.shape[:2]
    cx, cy = w // 2, h // 2

    # 中心に十字線を描画
    color = (0, 255, 0)  # 緑
    cv2.line(img, (cx - 30, cy), (cx + 30, cy), color, 1)
    cv2.line(img, (cx, cy - 30), (cx, cy + 30), color, 1)

    # オフセット情報を表示
    if offset is not None:
        text = f"Offset: ({offset.x:.3f}, {offset.y:.3f}) mm"
        cv2.putText(img, text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
        dist_text = f"Distance: {offset.distance:.3f} mm"
        cv2.putText(img, dist_text, (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)

    return Image(img)


def main() -> None:
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

    # 初期状態でReference Point検出確認
    print("\n=== 初期検出確認 ===")
    offset = detector.detect_with_statistics(camera.capture() for _ in range(30))

    if not offset:
        print("Reference Pointを検出できませんでした")
        print("カメラ視野内にReference Pointがあることを確認してください")
        return

    print(f"検出成功サンプル数: {offset.sample_count}")
    print(
        f"オフセット: ({offset.mean_mm.x:.3f}±{offset.std_mm.x:.3f}, {offset.mean_mm.y:.3f}±{offset.std_mm.y:.3f}) mm"
    )
    o1 = offset.mean_mm

    # カメラ回転角の計測（2点法）
    print("\n=== カメラ回転角の計測 ===")
    # 関心領域の80%の範囲で移動（はみ出し防止）
    crop_size_mm = Point2D(
        x=cam_config.crop.width / calibration.pixel_per_mm,
        y=cam_config.crop.height / calibration.pixel_per_mm,
    )
    move_distance = min(crop_size_mm.x, crop_size_mm.y) * 0.8 / 2  # 片方向の移動量

    # X方向に移動して計測
    move_vector = Point2D(x=move_distance, y=0.0)
    target_x = current_pos.x + move_vector.x
    print(f"X方向に {move_vector.x:.3f}mm 移動")
    klipper.send_gcode(gcode.move(x=target_x) + gcode.wait_for_done())
    time.sleep(0.5)

    offset_x = detector.detect_with_statistics(camera.capture() for _ in range(30))
    if not offset_x:
        print("移動後のReference Point検出に失敗しました")
        klipper.send_gcode("M84")
        return
    o2 = offset_x.mean_mm
    print(
        f"オフセット: ({o2.x:.3f}±{offset_x.std_mm.x:.3f}, {o2.y:.3f}±{offset_x.std_mm.y:.3f}) mm"
    )

    # 差分ベクトルから回転角を計算
    # theta = atan2(cross(m, d), dot(m, d))
    d = o2 - o1
    cross_md = move_vector.x * d.y - move_vector.y * d.x  # 2D外積（スカラー）
    dot_md = move_vector.x * d.x + move_vector.y * d.y  # 内積
    theta_rad = math.atan2(cross_md, dot_md)
    theta_deg = math.degrees(theta_rad)
    print(f"カメラ回転角: {theta_deg:.4f}° ({theta_rad:.6f} rad)")

    # 元の位置に戻る
    klipper.send_gcode(
        gcode.move(x=current_pos.x, y=current_pos.y) + gcode.wait_for_done()
    )
    time.sleep(0.5)

    # 位置合わせループ
    print("\n=== カメラ中心を基準点に合わせる ===")
    print("(qキーで中断)")
    cos_t = math.cos(theta_rad)
    sin_t = math.sin(theta_rad)
    max_iterations = 10

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_AUTOSIZE)
    machine_offset: Point2D | None = None

    try:
        for iteration in range(max_iterations):
            print(f"\n--- 試行 {iteration + 1}/{max_iterations} ---")

            # 統計計測中も映像を表示
            frames: list[Image] = []
            for _ in range(30):
                frame = camera.capture()
                frames.append(frame)
                display = draw_crosshair(
                    frame.crop_center(cam_config.crop.size), machine_offset
                )
                cv2.imshow(WINDOW_NAME, display.numpy())
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    print("中断しました")
                    return

            offset_final = detector.detect_with_statistics(iter(frames))
            if not offset_final:
                print("Reference Point検出に失敗しました")
                return

            # 回転角を考慮してオフセットを機械座標系に変換
            o = offset_final.mean_mm
            # カメラ座標→機械座標の変換（回転行列）
            machine_offset_x = cos_t * o.x - sin_t * o.y
            machine_offset_y = sin_t * o.x + cos_t * o.y
            machine_offset = Point2D(x=machine_offset_x, y=machine_offset_y)

            print(f"オフセット(カメラ): ({o.x:.3f}, {o.y:.3f}) mm")
            print(
                f"オフセット(機械): ({machine_offset.x:.3f}, {machine_offset.y:.3f}) mm"
            )
            print(f"距離: {machine_offset.distance:.3f} mm")

            # 許容誤差内なら終了
            if machine_offset.distance < args.tolerance:
                print(f"許容誤差 {args.tolerance}mm 以内に収束しました")
                break

            # 現在位置を取得して補正
            pos = stage.get_position()
            aligned_x = pos.x - machine_offset_x
            aligned_y = pos.y - machine_offset_y
            print(
                f"移動: ({pos.x:.3f}, {pos.y:.3f}) -> ({aligned_x:.3f}, {aligned_y:.3f})"
            )

            klipper.send_gcode(
                gcode.move(x=aligned_x, y=aligned_y) + gcode.wait_for_done()
            )
            time.sleep(0.5)
        else:
            print(f"\n{max_iterations}回の試行で収束しませんでした")

        print("\n位置合わせ完了")

        # 完了後も映像を表示し続ける（何かキーを押すまで）
        print("何かキーを押すと終了します...")
        while True:
            frame = camera.capture()
            display = draw_crosshair(
                frame.crop_center(cam_config.crop.size), machine_offset
            )
            cv2.imshow(WINDOW_NAME, display.numpy())
            if cv2.waitKey(100) != -1:
                break

    finally:
        klipper.send_gcode("M84")
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
