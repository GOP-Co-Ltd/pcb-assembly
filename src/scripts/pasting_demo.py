#!/usr/bin/env python3
"""ペースト塗布のデモスクリプト.

ホーミング → 位置合わせ → 高さ計測 → ローディング → 円塗布の一連のワークフローを実行する。
"""

import argparse
import logging
from pathlib import Path

from shapely import Point as ShapelyPoint

from pcb_assembly import gcode
from pcb_assembly.config import Machine
from pcb_assembly.control.pasting import PasteApplicator
from pcb_assembly.geometry import HeightMap, Move, Point2d, Transform
from pcb_assembly.hal import (
    NOZZLE_SPECS,
    Klipper,
    PasteDispenser,
    XYZStage,
)
from pcb_assembly.utils import setup_logging

PROJECT_ROOT = Path(__file__).parent.parent.parent


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


def interactive_positioning(
    klipper: Klipper,
    stage: XYZStage,
    toolhead: Transform,
) -> Point2d:
    """対話的に位置合わせを行い、機械座標を返す.

    Args:
        klipper: Klipperクライアント
        stage: XYZステージ
        toolhead: toolheadオフセット変換

    Returns:
        最後に移動した機械座標
    """
    print("\n座標を入力して位置を合わせます。計測するには 'q' を入力してください。")
    last_target: Point2d | None = None

    while True:
        try:
            raw = input("\nX Y (mm) > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if raw.lower() == "q":
            break
        parts = raw.split()
        if len(parts) != 2:
            print("X と Y をスペース区切りで入力してください（例: 100 200）")
            continue
        try:
            x, y = float(parts[0]), float(parts[1])
        except ValueError:
            print("数値を入力してください")
            continue

        target = toolhead.apply(Point2d(x, y))
        print(f"=== 移動: X={target.x}, Y={target.y} (toolheadオフセット適用) ===")
        klipper.send_gcode(
            stage.to_gcode(Move(x=target.x, y=target.y)) + gcode.wait_for_done()
        )
        last_target = target

    if last_target is None:
        raise RuntimeError("位置が入力されていません")
    return last_target


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
        "--amount", type=float, default=1.0, help="デフォルトの押し出し量 [μL]"
    )
    parser.add_argument("--radius", type=float, default=3.0, help="塗布円の半径 [mm]")
    parser.add_argument(
        "--height-map",
        type=Path,
        required=True,
        help="HeightMapのJSONファイルパス",
    )
    args = parser.parse_args()

    # ハードウェア初期化
    machine = Machine(args.config)
    klipper_config = machine.klipper
    dispenser_config = machine.paste_dispenser

    klipper = Klipper(host=klipper_config.host, port=klipper_config.port)
    stage = XYZStage(klipper.readonly)
    paste_dispenser = PasteDispenser(
        klipper=klipper.readonly,
        syringe_size=dispenser_config.syringe_size,
    )
    nozzle_spec = NOZZLE_SPECS[dispenser_config.nozzle_size]

    try:
        # 1. ホーミング
        print("=== ホーミング ===")
        klipper.send_gcode(gcode.homing(x=True, y=True, z=True) + gcode.wait_for_done())
        print("ホーミング完了")

        # 2. 対話的位置合わせ
        toolhead = machine.paste_dispenser.toolhead.to_transform()
        target = interactive_positioning(klipper, stage, toolhead)

        # 3. HeightMap読み込み
        print("\n=== HeightMap読み込み ===")
        height_transform = HeightMap.load(args.height_map)
        print(f"HeightMap: {args.height_map}")

        # 4. PasteApplicator作成
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

        # 5. 対話的ローディング
        interactive_loading(applicator, args.amount)

        # 6. リトラクション
        print("\n=== リトラクション ===")
        applicator.retract()
        print("リトラクション完了")

        # 7. 円塗布
        print(f"\n=== 円塗布 (半径: {args.radius} mm) ===")
        circle = ShapelyPoint(target.x, target.y).buffer(args.radius)
        applicator.apply([circle])
        print("塗布完了")

        klipper.send_gcode(gcode.relax())
        print("終了")
    except KeyboardInterrupt:
        print("\n=== 緊急停止 ===")
        klipper.emergency_stop()


if __name__ == "__main__":
    main()
