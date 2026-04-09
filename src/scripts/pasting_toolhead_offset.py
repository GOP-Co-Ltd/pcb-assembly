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

from pcb_assembly import gcode
from pcb_assembly.config import get_machine_config
from pcb_assembly.control.adjust import XYPositionAdjustor
from pcb_assembly.control.pasting import (
    PasteApplicator,
    ToolheadOffsetResult,
    interactive_loading,
)
from pcb_assembly.control.probe import ProbeExecutor
from pcb_assembly.control.setup import (
    OffsetObserver,
    machine_session,
    setup_board_calibration,
)
from pcb_assembly.geometry import Identity, Move, Point2d
from pcb_assembly.hal import PasteDispenser, ProbeSensor
from pcb_assembly.utils import PROJECT_ROOT, setup_logging
from pcb_assembly.vision import CircleDetector

WINDOW_NAME = "Toolhead Offset"


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(description="ツールヘッドオフセット自動計測")
    parser.add_argument(
        "--machine",
        "-m",
        type=str,
        default="pd_china_frame",
        help="マシン名",
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
        "--dispense-amount",
        type=float,
        default=5,
        help="吐出量 (uL)",
    )
    parser.add_argument(
        "--loading-amount",
        type=float,
        default=10.0,
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
        config_name = args.machine
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = PROJECT_ROOT / "data" / "toolhead_offset" / config_name
        output_dir.mkdir(parents=True, exist_ok=True)
        args.output = output_dir / f"{timestamp}.json"

    # === Phase 1: 初期化 & ボードキャリブレーション ===

    machine = get_machine_config(args.machine)
    cal_result = setup_board_calibration(
        machine=machine,
        pcb_file_path=args.pcb_file,
        tolerance=args.tolerance,
        window_name=WINDOW_NAME,
    )

    klipper = cal_result.klipper
    stage = cal_result.stage
    machine = cal_result.machine
    outline = cal_result.pcb.outline
    calibration = cal_result.calibration
    cam_config = machine.camera

    probe = ProbeSensor(klipper.readonly)
    probe_executor = ProbeExecutor(klipper=klipper, probe=probe, stage=stage)

    dispenser_config = machine.paste_dispenser
    paste_dispenser = PasteDispenser(
        klipper=klipper.readonly,
        rotations_per_ul=dispenser_config.rotations_per_ul,
    )

    with machine_session(klipper):
        board_transform = cal_result.board_transform

        # === Phase 2: ボード中央へツールヘッド移動 & プローブ ===
        print("\n=== ボード中央へ移動 & プローブ ===")
        board_center = Point2d(outline.width / 2, outline.height / 2)
        center_camera = board_transform.apply(board_center)
        toolhead_shift = machine.paste_dispenser.toolhead.to_transform()
        center_toolhead = toolhead_shift.apply(center_camera)

        # XY移動 -> center_toolhead
        klipper.send_gcode(
            stage.to_gcode(Move(x=center_toolhead.x, y=center_toolhead.y))
            + gcode.wait_for_done()
        )

        # PROBE -> board_surface_z 取得
        board_surface_z = probe_executor.probe()
        print(f"Board surface Z: {board_surface_z:.4f} mm")

        # === Phase 3: ペーストロード（対話式） ===
        print("\n=== ペーストロード ===")
        klipper.send_gcode(
            stage.to_gcode(
                Move(z=0.0)  # ツールヘッドを上げる
            )
            + gcode.wait_for_done()
        )

        with PasteApplicator(
            klipper=klipper,
            paste_dispenser=paste_dispenser,
            stage=stage,
            dispense_rate=dispenser_config.dispense_rate,
            dispense_accel=dispenser_config.dispense_accel,
            ul_per_mm2=dispenser_config.ul_per_mm2,
            retraction=dispenser_config.retract_amount,
            retraction_rate=dispenser_config.retract_rate,
            retraction_accel_factor=dispenser_config.retract_accel_factor,
            paste_height=dispenser_config.paste_height,
            lift_height=args.lift_height,
            transform=Identity(),
        ) as applicator:
            interactive_loading(applicator, args.loading_amount)

            # リトラクション
            print("\n=== リトラクション ===")
            applicator.retract()
            print("リトラクション完了")

        # === Phase 4: ペースト吐出 ===
        print("\n=== ペースト吐出 ===")
        dispense_z = board_surface_z + dispenser_config.paste_height

        # ステージを center_toolhead XY, Z=dispense_z へ移動
        klipper.send_gcode(
            stage.to_gcode(Move(x=center_toolhead.x, y=center_toolhead.y, z=dispense_z))
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
            stage.to_gcode(Move(z=dispense_z + args.lift_height))
            + gcode.wait_for_done()
        )

        print(
            f"吐出位置 (ステージ): ({center_toolhead.x:.3f}, {center_toolhead.y:.3f})"
        )

        # === Phase 5: ペースト検出 & 位置合わせ ===
        print("\n=== ペースト検出 & 位置合わせ ===")

        # ステージを center_camera 付近へ XY 移動
        klipper.send_gcode(
            stage.to_gcode(Move(x=center_camera.x, y=center_camera.y))
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
        paste_observer = OffsetObserver(
            detector=paste_detector,
            camera=cal_result.camera,
            crop_size=cam_config.crop.size,
            window_name=WINDOW_NAME,
        )

        def corrected_paste_offset() -> Point2d:
            return cal_result.offset_transform.apply(paste_observer())

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
            center_toolhead.x - camera_final_pos.x,
            center_toolhead.y - camera_final_pos.y,
        )

        offset_result = ToolheadOffsetResult(
            offset=measured_offset,
            dispense_position=center_toolhead,
            camera_position=camera_final_pos,
            tolerance=args.tolerance,
            calibrated_at=datetime.now(),
        )

        # 保存
        args.output.parent.mkdir(parents=True, exist_ok=True)
        offset_result.save(args.output)
        print(f"結果を保存しました: {args.output}")

        # 結果表示
        print("\n=== 計測結果 ===")
        print(f"計測オフセット: X={measured_offset.x:.4f} Y={measured_offset.y:.4f}")
        print()
        print("machine.tomlに設定する値:")
        print("[paste_dispenser.toolhead]")
        print(f"x = {measured_offset.x}")
        print(f"y = {measured_offset.y}")

        # 現在設定値との差分
        current_toolhead = machine.paste_dispenser.toolhead
        diff_x = measured_offset.x - current_toolhead.x
        diff_y = measured_offset.y - current_toolhead.y
        print()
        print(f"現在設定値: X={current_toolhead.x:.4f} Y={current_toolhead.y:.4f}")
        print(f"差分: dX={diff_x:.4f} dY={diff_y:.4f}")


if __name__ == "__main__":
    main()
