#!/usr/bin/env python3
"""ペースト塗布のデモスクリプト.

ボード計測ベースのフロー:
1. 設定読み込み・PCBファイル読み込み
2. Klipper接続・カメラ初期化
3. ホーミング
4. Reference Point へ移動・位置調整
5. カメラ回転角の計測（OffsetTransformMeasurer）
6. Board変換の計測（BoardTransformMeasurer）
7. HeightMap読み込み
8. ボード中央座標を計算 → toolheadオフセット適用で機械座標に変換
9. PasteApplicator作成
10. ローディング → リトラクション → 円塗布
"""

import argparse
import logging
import time
from pathlib import Path

import cv2
from shapely import Point as ShapelyPoint

from pcb_assembly import gcode
from pcb_assembly.config import Machine
from pcb_assembly.control.adjust import (
    BoardTransformMeasurer,
    OffsetTransformMeasurer,
    XYPositionAdjustor,
)
from pcb_assembly.control.pasting import PasteApplicator
from pcb_assembly.geometry import Compose, HeightMap, Move, Point2d
from pcb_assembly.hal import (
    NOZZLE_SPECS,
    Klipper,
    PasteDispenser,
    XYZStage,
    create_camera,
)
from pcb_assembly.pcb import PcbFile
from pcb_assembly.utils import setup_logging
from pcb_assembly.vision import (
    CalibrationResult,
    CircleDetector,
    Image,
    safe_move_distance,
)

PROJECT_ROOT = Path(__file__).parent.parent.parent
WINDOW_NAME = "Pasting Demo"


def draw_overlay(
    image: Image,
    crop_size: tuple[int, int],
    offset: Point2d | None = None,
) -> Image:
    """画像に十字線、関心領域、オフセット情報を描画する."""
    img = image.numpy().copy()
    h, w = img.shape[:2]
    cx, cy = w // 2, h // 2

    color = (0, 255, 0)

    cv2.line(img, (cx - 30, cy), (cx + 30, cy), color, 1)
    cv2.line(img, (cx, cy - 30), (cx, cy + 30), color, 1)

    half_w, half_h = crop_size[0] // 2, crop_size[1] // 2
    x1, y1 = cx - half_w, cy - half_h
    x2, y2 = cx + half_w, cy + half_h
    cv2.rectangle(img, (x1, y1), (x2, y2), color, 1)

    if offset is not None:
        text = f"Offset: ({offset.x:.3f}, {offset.y:.3f}) mm"
        cv2.putText(img, text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
        dist_text = f"Distance: {offset.norm:.3f} mm"
        cv2.putText(img, dist_text, (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)

    return Image(img)


def interactive_loading(applicator: PasteApplicator, default_amount: float) -> None:
    """対話的にペーストをローディングする.

    Args:
        applicator: ペーストアプリケーター
        default_amount: デフォルトの押し出し量 [uL]
    """
    print(f"ローディング (デフォルト量: {default_amount} uL)")
    print("Enter: デフォルト量を押し出し, 数値: その量を押し出し, q: 終了")
    print()

    while True:
        cmd = input("> ").strip()

        match cmd:
            case "q" | "quit":
                break
            case "":
                print(f"ローディング: {default_amount} uL")
                applicator.load(default_amount)
                print("完了")
            case _:
                try:
                    amount = float(cmd)
                except ValueError:
                    print("不正な入力です")
                    continue
                print(f"ローディング: {amount} uL")
                applicator.load(amount)
                print("完了")


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(description="ペースト塗布デモ")
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
    parser.add_argument(
        "--amount", type=float, default=1.0, help="デフォルトの押し出し量 [uL]"
    )
    parser.add_argument("--radius", type=float, default=3.0, help="塗布円の半径 [mm]")
    parser.add_argument(
        "--height-map",
        type=Path,
        required=True,
        help="HeightMapのJSONファイルパス",
    )
    args = parser.parse_args()

    # 設定読み込み
    print("=== 設定読み込み ===")
    machine = Machine(args.config)

    # PCBファイル読み込み
    print("\n=== PCBファイル読み込み ===")
    pcb = PcbFile(args.pcb_file)
    outline = pcb.outline
    print(f"Board幅: {outline.width:.3f} mm, 高さ: {outline.height:.3f} mm")

    # Klipper接続
    print("\n=== Klipper接続 ===")
    klipper_config = machine.klipper
    dispenser_config = machine.paste_dispenser

    klipper = Klipper(host=klipper_config.host, port=klipper_config.port)
    stage = XYZStage(klipper.readonly)
    paste_dispenser = PasteDispenser(
        klipper=klipper.readonly,
        syringe_size=dispenser_config.syringe_size,
    )
    nozzle_spec = NOZZLE_SPECS[dispenser_config.nozzle_size]

    # カメラ初期化
    print("\n=== カメラ初期化 ===")
    cam_config = machine.camera
    camera = create_camera(
        device_id=cam_config.device_id,
        width=cam_config.width,
        height=cam_config.height,
        fps=cam_config.fps,
        format=cam_config.format,
        backend=cam_config.backend,
    )

    # キャリブレーション結果読み込み
    calibration = CalibrationResult.load(cam_config.calibration_file)

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
    klipper.send_gcode(gcode.homing(x=True, y=True, z=True) + gcode.wait_for_done())
    print("ホーミング完了")

    # Reference Pointへ移動
    print("\n=== Reference Point (top left) へ移動 ===")
    klipper.send_gcode(
        stage.to_gcode(Move(x=ref_config.x, y=ref_config.y)) + gcode.wait_for_done()
    )
    time.sleep(1.0)

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

    # カメラ回転角の計測
    print("\n=== カメラ回転角の計測 ===")
    move_distance = (
        safe_move_distance(cam_config.crop.size, margin=0.3) / calibration.pixel_per_mm
    )
    offset_transform_measurer = OffsetTransformMeasurer(
        observe_offset=observe_offset,
        klipper=klipper,
        stage=stage,
        move_distance=move_distance,
    )
    offset_transform = offset_transform_measurer.measure()

    def corrected_offset() -> Point2d:
        return offset_transform.apply(observe_offset())

    position_adjustor = XYPositionAdjustor(
        observe_offset=corrected_offset,
        klipper=klipper,
        stage=stage,
        tolerance=args.tolerance,
    )

    def adjust_reference() -> Point2d:
        return position_adjustor.adjust()

    # Board変換の計測
    print("\n=== Board変換の計測 ===")
    board_transform_measurer = BoardTransformMeasurer(
        adjust_reference=adjust_reference,
        klipper=klipper,
        stage=stage,
        outline=outline,
        reference_point=ref_config,
    )

    try:
        board_transform = board_transform_measurer.measure()

        # HeightMap読み込み
        print("\n=== HeightMap読み込み ===")
        height_transform = HeightMap.load(args.height_map)
        print(f"HeightMap: {args.height_map}")

        # ボード中央座標を計算
        board_center = Point2d(outline.width / 2, outline.height / 2)
        toolhead_offset = machine.paste_dispenser.toolhead.to_transform()
        center_machine = Compose([board_transform, toolhead_offset]).apply(board_center)
        print(
            f"ボード中央 (機械座標): X={center_machine.x:.3f}, Y={center_machine.y:.3f}"
        )

        # PasteApplicator作成
        applicator = PasteApplicator(
            klipper=klipper,
            paste_dispenser=paste_dispenser,
            stage=stage,
            nozzle_spec=nozzle_spec,
            paste_velocity=dispenser_config.paste_velocity,
            paste_thickness=dispenser_config.paste_thickness,
            retraction=dispenser_config.retract_amount,
            retraction_rate=dispenser_config.retract_rate,
            retraction_accel_factor=dispenser_config.retract_accel_factor,
            paste_accel=dispenser_config.dispense_accel,
            paste_height=dispenser_config.paste_height,
            transform=height_transform,
        )

        # 対話的ローディング
        interactive_loading(applicator, args.amount)

        # リトラクション
        print("\n=== リトラクション ===")
        applicator.retract()
        print("リトラクション完了")

        # 円塗布
        print(f"\n=== 円塗布 (半径: {args.radius} mm) ===")
        circle = ShapelyPoint(center_machine.x, center_machine.y).buffer(args.radius)
        applicator.apply([circle])
        print("塗布完了")

    except KeyboardInterrupt:
        print("\n=== 緊急停止 ===")
        klipper.emergency_stop()
    finally:
        klipper.send_gcode("M84")
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
