#!/usr/bin/env python3
"""Board座標→機械座標変換を計測するスクリプト.

処理順:
1. 設定読み込み・初期化
2. PCBファイルからOutlineを読み込み
3. G28でホーミング
4. Reference Point (top left) へ移動
5. カメラ回転角の計測（OffsetTransformMeasurer）
6. Board変換の計測（BoardTransformMeasurer）
7. 結果表示
"""

import argparse
import logging
import time
from pathlib import Path

import cv2

from pcb_assembly import gcode
from pcb_assembly.config import Machine
from pcb_assembly.control.adjust import (
    BoardTransformMeasurer,
    OffsetTransformMeasurer,
    XYPositionAdjustor,
)
from pcb_assembly.geometry import Point2d, sort_by_nearest
from pcb_assembly.hal import Camera, Klipper, XYZStage
from pcb_assembly.pcb import Layer, PcbFile
from pcb_assembly.utils import setup_logging
from pcb_assembly.vision import (
    CalibrationResult,
    CircleDetector,
    Image,
    safe_move_distance,
)

PROJECT_ROOT = Path(__file__).parent.parent.parent
WINDOW_NAME = "Board Transform Measurement"


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
    parser = argparse.ArgumentParser(description="Board座標→機械座標変換を計測する")
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
        default=0.01,
        help="位置合わせの許容誤差 (mm)",
    )
    args = parser.parse_args()

    # 設定読み込み
    print("=== 設定読み込み ===")
    machine = Machine(args.config)
    print(f"設定ファイル: {args.config}")

    # PCBファイル読み込み
    print("\n=== PCBファイル読み込み ===")
    pcb = PcbFile(args.pcb_file)
    outline = pcb.outline
    print(f"PCBファイル: {args.pcb_file}")
    print(f"Board幅: {outline.width:.3f} mm")
    print(f"Board高さ: {outline.height:.3f} mm")

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
    print("\n=== Reference Point (top left) へ移動 ===")
    print(f"目標位置: ({ref_config.x}, {ref_config.y})")
    klipper.send_gcode(
        gcode.move(x=ref_config.x, y=ref_config.y, velocity=20) + gcode.wait_for_done()
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
    offset_transform = offset_transform_measurer.measure(observe_offset, klipper, stage)

    # 補正済みオフセット関数を定義
    def corrected_offset() -> Point2d:
        return offset_transform.apply(observe_offset())

    # 位置補正を行い最終座標を返す関数を定義
    position_adjustor = XYPositionAdjustor(tolerance=args.tolerance)

    def adjust_reference() -> Point2d:
        return position_adjustor.adjust(corrected_offset, klipper, stage)

    # Board変換の計測
    print("\n=== Board変換の計測 ===")
    board_transform_measurer = BoardTransformMeasurer(
        outline=outline,
        reference_point=ref_config,
    )

    try:
        board_transform = board_transform_measurer.measure(
            adjust_reference,
            klipper,
            stage,
        )

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
                board_pt = center_3d.to2d()
                machine_pt = board_transform.apply(board_pt)

                # 移動
                klipper.send_gcode(
                    gcode.move(x=machine_pt.x, y=machine_pt.y, velocity=30)
                    + gcode.wait_for_done()
                )

                # カメラ表示
                for _ in range(int(camera.resolution.fps * 0.5)):
                    frame = camera.capture()
                    img = draw_overlay(frame, cam_config.crop.size).numpy()

                    # パッド情報をオーバーレイ
                    info_text = f"Pad {i + 1}/{len(sorted_centers)}"
                    cv2.putText(
                        img,
                        info_text,
                        (10, 90),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.7,
                        (0, 255, 0),
                        2,
                    )
                    cv2.imshow(WINDOW_NAME, img)

                    # Escキーで中断
                    if cv2.waitKey(1) == 27:  # Esc
                        print("中断しました")
                        break

            print("巡回完了")
        else:
            print("巡回するパッドがありません")

        # ボード左上 (0, 0) に移動
        origin_machine = board_transform.apply(Point2d(0.0, 0.0))
        klipper.send_gcode(
            gcode.move(x=origin_machine.x, y=origin_machine.y, velocity=30)
            + gcode.wait_for_done()
        )

        # 完了後も映像を表示し続ける（何かキーを押すまで）
        print("\n何かキーを押すと終了します...")
        while True:
            frame = camera.capture()
            display = draw_overlay(frame, cam_config.crop.size)
            cv2.imshow(WINDOW_NAME, display.numpy())
            if cv2.waitKey(100) != -1:
                break

    finally:
        klipper.send_gcode("M84")
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
