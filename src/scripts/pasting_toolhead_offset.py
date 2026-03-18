#!/usr/bin/env python3
"""ツールヘッドオフセットの自動計測スクリプト.

ペーストを吐出してCircleDetectorで検出することで、
カメラ-ツールヘッド間のXYオフセットを自動計測する。

処理順:
1. 設定読み込み・初期化
2. G28ホーミング
3. Reference Pointへ移動・位置調整
4. カメラ回転角の計測（OffsetTransformMeasurer）
5. Board変換の計測（BoardTransformMeasurer）
6. ボード中央へツールヘッド移動・プローブ
7. ペーストロード（対話式）
8. ペースト吐出
9. ペースト検出・位置合わせ
10. オフセット算出・保存
"""

import argparse
import logging
import time
from datetime import datetime
from pathlib import Path

import cv2

from pcb_assembly import gcode
from pcb_assembly.config import Machine
from pcb_assembly.control.adjust import (
    BoardTransformMeasurer,
    OffsetTransformMeasurer,
    XYPositionAdjustor,
)
from pcb_assembly.control.pasting import PasteApplicator, ToolheadOffsetResult
from pcb_assembly.geometry import Identity, Point2d
from pcb_assembly.hal import (
    NOZZLE_SPECS,
    Klipper,
    PasteDispenser,
    ProbeSensor,
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
WINDOW_NAME = "Toolhead Offset"


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
        default_amount: デフォルトの押し出し量 [μL]
    """
    print(f"ローディング (デフォルト量: {default_amount} μL)")
    print("Enter: デフォルト量を押し出し, 数値: その量を押し出し, q: 終了")
    print()

    while True:
        cmd = input("> ").strip()

        match cmd:
            case "q" | "quit":
                break
            case "":
                print(f"ローディング: {default_amount} μL")
                applicator.load(default_amount)
                print("完了")
            case _:
                try:
                    amount = float(cmd)
                except ValueError:
                    print("不正な入力です")
                    continue
                print(f"ローディング: {amount} μL")
                applicator.load(amount)
                print("完了")


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(description="ツールヘッドオフセット自動計測")
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
        default=0.05,
        help="位置合わせの許容誤差 (mm)",
    )
    parser.add_argument(
        "--dispense-height",
        type=float,
        default=0.1,
        help="ボード面からの吐出高さ (mm)",
    )
    parser.add_argument(
        "--dispense-amount",
        type=float,
        default=0.5,
        help="吐出量 (uL)",
    )
    parser.add_argument(
        "--loading-amount",
        type=float,
        default=1.0,
        help="ローディングデフォルト量 (uL)",
    )
    parser.add_argument(
        "--lift-height",
        type=float,
        default=5.0,
        help="吐出後のZ持ち上げ高さ (mm)",
    )
    parser.add_argument(
        "--paste-diameter-min",
        type=float,
        default=0.0,
        help="検出する円の最小直径 (mm)",
    )
    parser.add_argument(
        "--paste-diameter-max",
        type=float,
        default=1.0,
        help="検出する円の最大直径 (mm)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="出力パス (省略時: data/toolhead_offset/<config名>/<時刻>.json)",
    )
    args = parser.parse_args()

    # デフォルト出力先の生成
    if args.output is None:
        config_name = Path(args.config).parent.name
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = PROJECT_ROOT / "data" / "toolhead_offset" / config_name
        output_dir.mkdir(parents=True, exist_ok=True)
        args.output = output_dir / f"{timestamp}.json"

    # === Phase 1: 初期化 & ボードキャリブレーション ===

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
    klipper = Klipper(host=machine.klipper.host, port=machine.klipper.port)
    stage = XYZStage(klipper.readonly)
    probe = ProbeSensor(klipper.readonly)

    # PasteDispenser初期化
    dispenser_config = machine.paste_dispenser
    paste_dispenser = PasteDispenser(
        klipper=klipper.readonly,
        syringe_size=dispenser_config.syringe_size,
    )

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

    # 基準点用円検出器初期化
    ref_config = machine.reference_point
    ref_detector = CircleDetector(
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
    print("\n=== Reference Point へ移動 ===")
    klipper.send_gcode(
        gcode.move(x=ref_config.x, y=ref_config.y, velocity=20) + gcode.wait_for_done()
    )
    time.sleep(1.0)

    # オフセット検出関数を定義
    sample_count = 30
    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_AUTOSIZE)

    def observe_ref_offset() -> Point2d:
        result = ref_detector.detect_with_statistics(
            camera.capture() for _ in range(sample_count)
        )
        if result is None:
            raise RuntimeError("基準点の検出に失敗しました")

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
        observe_offset=observe_ref_offset,
        klipper=klipper,
        stage=stage,
        move_distance=move_distance,
    )
    offset_transform = offset_transform_measurer.measure()

    def corrected_ref_offset() -> Point2d:
        return offset_transform.apply(observe_ref_offset())

    position_adjustor = XYPositionAdjustor(
        observe_offset=corrected_ref_offset,
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

        # === Phase 2: ボード中央へツールヘッド移動 & プローブ ===
        print("\n=== ボード中央へ移動 & プローブ ===")
        board_center = Point2d(outline.width / 2, outline.height / 2)
        center_camera = board_transform.apply(board_center)
        toolhead_shift = machine.toolhead.to_transform()
        center_toolhead = toolhead_shift.apply(center_camera)

        # XY移動 → center_toolhead
        klipper.send_gcode(
            gcode.move(x=center_toolhead.x, y=center_toolhead.y, velocity=20)
            + gcode.wait_for_done()
        )

        # PROBE_ACCURACY REPEAT=10 → board_surface_z 取得
        klipper.send_gcode(
            gcode.GCode("PROBE_ACCURACY REPEAT=10")
            + gcode.wait(1.0)
            + gcode.wait_for_done()
        )
        board_surface_z = probe.get_last_z_result()
        print(f"Board surface Z: {board_surface_z:.4f} mm")

        # === Phase 3: ペーストロード（対話式） ===
        print("\n=== ペーストロード ===")
        nozzle_spec = NOZZLE_SPECS[dispenser_config.nozzle_size]
        applicator = PasteApplicator(
            klipper=klipper,
            paste_dispenser=paste_dispenser,
            stage=stage,
            nozzle_spec=nozzle_spec,
            paste_velocity=5.0,
            paste_thickness=0.1,
            retraction=dispenser_config.retract_amount,
            retraction_rate=dispenser_config.retract_rate,
            retraction_accel_factor=dispenser_config.retract_accel_factor,
            paste_accel=dispenser_config.dispense_accel,
            paste_height=args.dispense_height,
            lift_height=args.lift_height,
            transform=Identity(),
        )

        interactive_loading(applicator, args.loading_amount)

        # リトラクション
        print("\n=== リトラクション ===")
        applicator.retract()
        print("リトラクション完了")

        # === Phase 4: ペースト吐出 ===
        print("\n=== ペースト吐出 ===")
        dispense_z = board_surface_z + args.dispense_height

        # ステージを center_toolhead XY, Z=dispense_z へ移動
        klipper.send_gcode(
            gcode.move(
                x=center_toolhead.x, y=center_toolhead.y, z=dispense_z, velocity=20
            )
            + gcode.wait_for_done()
        )

        # 吐出
        klipper.send_gcode(
            paste_dispenser.pushpull(
                args.dispense_amount,
                dispenser_config.dispense_rate,
                dispenser_config.dispense_accel,
            )
            + gcode.wait_for_done()
        )

        # リトラクション
        retract_accel = (
            dispenser_config.dispense_accel * dispenser_config.retract_accel_factor
        )
        klipper.send_gcode(
            paste_dispenser.pushpull(
                -dispenser_config.retract_amount,
                dispenser_config.retract_rate,
                retract_accel,
            )
            + gcode.wait_for_done()
        )

        # Z を lift_height 分持ち上げ
        klipper.send_gcode(
            gcode.move(z=dispense_z + args.lift_height, velocity=20)
            + gcode.wait_for_done()
        )

        dispense_stage_pos = center_toolhead
        print(
            f"吐出位置 (ステージ): ({dispense_stage_pos.x:.3f}, {dispense_stage_pos.y:.3f})"
        )

        # === Phase 5: ペースト検出 & 位置合わせ ===
        print("\n=== ペースト検出 & 位置合わせ ===")

        # ステージを center_camera 付近へ XY 移動
        klipper.send_gcode(
            gcode.move(x=center_camera.x, y=center_camera.y, velocity=20)
            + gcode.wait_for_done()
        )
        time.sleep(1.0)

        # ペースト用 CircleDetector
        target_diameter_mm = (args.paste_diameter_min + args.paste_diameter_max) / 2
        diameter_tolerance_mm = (args.paste_diameter_max - args.paste_diameter_min) / 2
        paste_detector = CircleDetector(
            pixel_per_mm=calibration.pixel_per_mm,
            target_diameter_mm=target_diameter_mm,
            crop_size=cam_config.crop.size,
            diameter_tolerance_mm=diameter_tolerance_mm,
        )

        # offset_transform を再利用してカメラ回転補正
        def observe_paste_offset() -> Point2d:
            result = paste_detector.detect_with_statistics(
                camera.capture() for _ in range(sample_count)
            )
            if result is None:
                raise RuntimeError("ペーストドットの検出に失敗しました")

            display = draw_overlay(
                camera.capture(), cam_config.crop.size, result.mean_mm
            )
            cv2.imshow(WINDOW_NAME, display.numpy())
            cv2.waitKey(1)

            return result.mean_mm

        def corrected_paste_offset() -> Point2d:
            return offset_transform.apply(observe_paste_offset())

        # XYPositionAdjustor でペーストドット中心に自動位置合わせ
        paste_adjustor = XYPositionAdjustor(
            observe_offset=corrected_paste_offset,
            klipper=klipper,
            stage=stage,
            tolerance=args.tolerance,
        )
        camera_final_pos = paste_adjustor.adjust()
        print(f"カメラ最終位置: ({camera_final_pos.x:.3f}, {camera_final_pos.y:.3f})")

        # === Phase 6: オフセット算出 & 保存 ===
        print("\n=== オフセット算出 & 保存 ===")
        measured_offset = Point2d(
            dispense_stage_pos.x - camera_final_pos.x,
            dispense_stage_pos.y - camera_final_pos.y,
        )

        result = ToolheadOffsetResult(
            offset=measured_offset,
            dispense_position=dispense_stage_pos,
            camera_position=camera_final_pos,
            tolerance=args.tolerance,
            calibrated_at=datetime.now(),
        )

        # 保存
        args.output.parent.mkdir(parents=True, exist_ok=True)
        result.save(args.output)
        print(f"結果を保存しました: {args.output}")

        # 結果表示
        print("\n=== 計測結果 ===")
        print(f"計測オフセット: X={measured_offset.x:.4f} Y={measured_offset.y:.4f}")
        print()
        print("machine.tomlに設定する値:")
        print("[toolhead]")
        print(f"x = {measured_offset.x}")
        print(f"y = {measured_offset.y}")

        # 現在設定値との差分
        current_toolhead = machine.toolhead
        diff_x = measured_offset.x - current_toolhead.x
        diff_y = measured_offset.y - current_toolhead.y
        print()
        print(f"現在設定値: X={current_toolhead.x:.4f} Y={current_toolhead.y:.4f}")
        print(f"差分: dX={diff_x:.4f} dY={diff_y:.4f}")

    finally:
        klipper.send_gcode("M84")
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
