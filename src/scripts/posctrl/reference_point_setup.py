#!/usr/bin/env python3
"""基準点(top left)の絶対位置を計測し machine.toml へ記録するスクリプト.

circle_detection_demo を手動ジョグ運用に作り直したもの。検出結果は中央寄せの
目安として表示するだけで、記録するのはユーザーがジョグした実際のマシン座標。

処理順:
1. machine config 読み込み
2. カメラキャリブレーション読み込み
3. G28 ホーミング後、キャリブレーション Z 高さへ移動
4. ライブプレビュー(クロップ枠/十字/円検出オフセット)を表示しつつ、
   ユーザーが Klipper console で top left へジョグ
5. Enter で現在の x, y を machine.toml の [reference_point] へ上書き
"""

import argparse
import logging
import time
from pathlib import Path

import cv2
import tomlkit
from tomlkit.items import Table

from pcbasm import gcode
from pcbasm.config import Machine
from pcbasm.hal import Klipper, XYZStage, create_camera
from pcbasm.utils import PROJECT_ROOT, setup_logging
from pcbasm.vision import (
    CalibrationResult,
    CircleDetector,
    draw_detected_circle,
    draw_overlay,
)

logger = logging.getLogger(__name__)

WINDOW_NAME = "Reference Point Setup"
ENTER_KEYS = (10, 13)  # LF / CR
QUIT_KEYS = (ord("q"), 27)  # q / Esc


def update_reference_point(config_path: Path, x: float, y: float) -> None:
    """machine.toml の [reference_point] の x, y をコメントを保ったまま上書きする."""
    doc = tomlkit.parse(config_path.read_text())
    ref = doc["reference_point"]
    assert isinstance(ref, Table)
    ref["x"] = round(x, 3)
    ref["y"] = round(y, 3)
    config_path.write_text(tomlkit.dumps(doc))


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(description="基準点(top left)の位置記録")
    parser.add_argument(
        "--machine", "-m", type=str, default="kurousagi", help="マシン名"
    )
    args = parser.parse_args()

    config_path = PROJECT_ROOT / "configs" / args.machine / "machine.toml"
    machine = Machine(config_path)

    # Klipper 接続
    klipper = Klipper(host=machine.klipper.host, port=machine.klipper.port)
    stage = XYZStage(klipper.readonly)
    logger.info("Klipper: %s:%s", machine.klipper.host, machine.klipper.port)

    # カメラ・キャリブレーション
    cam_config = machine.camera
    camera = create_camera(
        device_id=cam_config.device_id,
        width=cam_config.width,
        height=cam_config.height,
        fps=cam_config.fps,
        format=cam_config.format,
        backend=cam_config.backend,
    )
    calibration = CalibrationResult.load(cam_config.calibration_file)
    logger.info(
        "カメラ: %s / pixel/mm: %.2f", camera.info.name, calibration.pixel_per_mm
    )

    ref_config = machine.reference_point
    detector = CircleDetector(
        pixel_per_mm=calibration.pixel_per_mm,
        target_diameter_mm=ref_config.target_diameter,
        crop_size=cam_config.crop.size,
        diameter_tolerance_mm=0.5,
    )

    # ホーミング
    logger.info("ホーミング (G28)...")
    klipper.send_gcode(gcode.homing(x=True, y=True, z=True) + gcode.wait_for_done())

    # カメラのピントが合うキャリブレーション Z へ移動
    if calibration.z_position is not None:
        logger.info("カメラ Z 高さへ移動: %.3f mm", calibration.z_position)
        klipper.send_gcode(stage.move(z=calibration.z_position) + gcode.wait_for_done())
    else:
        logger.warning("キャリブレーションに Z 位置がありません。Z は移動しません")
    time.sleep(1.0)

    print()
    print("=== top left へジョグしてください ===")
    print(f"  Klipper console: {machine.klipper.host}:{machine.klipper.port}")
    print("  プレビューの十字と基準点マーカーを合わせ、ウィンドウ上で操作:")
    print("    [Enter] 現在位置を記録   [q]/[Esc] 中止")
    print()

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_AUTOSIZE)
    console = f"{machine.klipper.host}:{machine.klipper.port}"

    recorded = False
    while True:
        image = camera.capture()
        result = detector.detect_nearest_center(image)
        offset = result.offset.mm if result is not None else None

        preview = draw_overlay(image, cam_config.crop.size, offset).numpy()
        if result is not None:
            draw_detected_circle(preview, result, cam_config.crop.size)

        pos = stage.get_position()
        lines = [
            f"Pos: X{pos.x:.3f}  Y{pos.y:.3f}",
            f"Jog via Klipper console: {console}",
            "[Enter] record   [q] quit",
        ]
        for i, text in enumerate(lines):
            cv2.putText(
                preview,
                text,
                (10, 100 + i * 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (0, 255, 255),
                2,
            )

        cv2.imshow(WINDOW_NAME, preview)
        key = cv2.waitKey(1) & 0xFF

        if key in ENTER_KEYS:
            pos = stage.get_position()
            update_reference_point(config_path, pos.x, pos.y)
            logger.info(
                "[reference_point] を更新: x=%.3f, y=%.3f -> %s",
                pos.x,
                pos.y,
                config_path,
            )
            recorded = True
            break
        if key in QUIT_KEYS:
            logger.info("中止しました。configは変更していません")
            break

    cv2.destroyAllWindows()
    klipper.send_gcode(gcode.relax())
    if recorded:
        print("記録完了")


if __name__ == "__main__":
    main()
