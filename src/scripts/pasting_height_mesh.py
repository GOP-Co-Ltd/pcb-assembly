#!/usr/bin/env python3
"""基板表面のbed meshを計測し、HeightMapを保存するスクリプト.

処理順:
1. 設定読み込み・初期化
2. PCBファイルからOutlineを読み込み
3. G28でホーミング
4. Reference Point (top left) へ移動・位置調整
5. カメラ回転角の計測（OffsetTransformMeasurer）
6. Board変換の計測（BoardTransformMeasurer）
7. Bed mesh計測（HeightTransformMeasurer）
8. HeightMapをファイルに保存
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
    HeightTransformMeasurer,
    OffsetTransformMeasurer,
    XYPositionAdjustor,
)
from pcb_assembly.control.probe import ProbeExecutor
from pcb_assembly.geometry import Compose, Point2d
from pcb_assembly.hal import Klipper, ProbeSensor, XYZStage, create_camera
from pcb_assembly.pcb import PcbFile
from pcb_assembly.utils import setup_logging
from pcb_assembly.vision import (
    CalibrationResult,
    CircleDetector,
    Image,
    safe_move_distance,
)

PROJECT_ROOT = Path(__file__).parent.parent.parent
WINDOW_NAME = "Height Mesh"


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


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(description="基板表面のbed mesh計測")
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
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="HeightMap保存先のJSONファイルパス (省略時: data/height_mesh/<config名>/<pcb名>_<時刻>.json)",
    )
    args = parser.parse_args()

    # デフォルト出力先の生成
    if args.output is None:
        config_name = Path(args.config).parent.name
        pcb_stem = Path(args.pcb_file).stem
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = PROJECT_ROOT / "data" / "height_mesh" / config_name
        output_dir.mkdir(parents=True, exist_ok=True)
        args.output = output_dir / f"{pcb_stem}_{timestamp}.json"

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
    probe_executor = ProbeExecutor(klipper=klipper, probe=probe, stage=stage)

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
        gcode.move(x=ref_config.x, y=ref_config.y, velocity=20) + gcode.wait_for_done()
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

        # Bed mesh計測
        print("\n=== Bed mesh計測 ===")
        toolhead_offset = machine.paste_dispenser.toolhead.to_transform()
        height_measurer = HeightTransformMeasurer(
            probe_executor=probe_executor,
            klipper=klipper,
            stage=stage,
        )
        height_map = height_measurer.measure(
            outline=outline,
            board_to_machine=Compose([board_transform, toolhead_offset]),
        )

        # 保存
        height_map.save(args.output)
        print(f"\nHeightMapを保存しました: {args.output}")

    finally:
        klipper.send_gcode("M84")
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
