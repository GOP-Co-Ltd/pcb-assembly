#!/usr/bin/env python3
"""CoreXY直行性テストスクリプト.

ペンをXYZステージのヘッドに固定し、紙上に正方形+対角線と円を描画して
ベルトテンション・直行性を目視検証する。

使い方:
1. ペンをヘッドに固定し、紙をステージに置く
2. スクリプトを起動（ホーミング実行）
3. 対話的に座標を入力 → その地点を中心にパターンを描画
"""

import argparse
import logging
import math
from pathlib import Path

from pcb_assembly import gcode
from pcb_assembly.config import Machine
from pcb_assembly.geometry import Move
from pcb_assembly.hal import Klipper, XYZStage
from pcb_assembly.utils import setup_logging

PROJECT_ROOT = Path(__file__).parent.parent.parent


def square_with_diagonals(
    cx: float,
    cy: float,
    size: float,
    lift: float,
    draw_v: float,
    travel_v: float,
) -> list[Move]:
    """正方形+対角線の描画Moveリストを生成する.

    ペンが上がった状態で開始・終了する前提。
    """
    h = size / 2
    tl_x, tl_y = cx - h, cy + h
    tr_x, tr_y = cx + h, cy + h
    br_x, br_y = cx + h, cy - h
    bl_x, bl_y = cx - h, cy - h

    return [
        # TLへ移動 → ペン下げ
        Move(x=tl_x, y=tl_y, v=travel_v),
        Move(z=-lift, relative=True, v=travel_v),
        # 正方形
        Move(x=tr_x, y=tr_y, v=draw_v),
        Move(x=br_x, y=br_y, v=draw_v),
        Move(x=bl_x, y=bl_y, v=draw_v),
        Move(x=tl_x, y=tl_y, v=draw_v),
        # 対角線1: TL → BR（既にTLにいる）
        Move(x=br_x, y=br_y, v=draw_v),
        # ペン上げ → TRへ → ペン下げ
        Move(z=lift, relative=True, v=travel_v),
        Move(x=tr_x, y=tr_y, v=travel_v),
        Move(z=-lift, relative=True, v=travel_v),
        # 対角線2: TR → BL
        Move(x=bl_x, y=bl_y, v=draw_v),
        # ペン上げ
        Move(z=lift, relative=True, v=travel_v),
    ]


def circle(
    cx: float,
    cy: float,
    diameter: float,
    lift: float,
    draw_v: float,
    travel_v: float,
    segments: int = 72,
) -> list[Move]:
    """円の描画Moveリストを生成する.

    ペンが上がった状態で開始・終了する前提。
    """
    radius = diameter / 2
    points = [
        (
            cx + radius * math.cos(2 * math.pi * i / segments),
            cy + radius * math.sin(2 * math.pi * i / segments),
        )
        for i in range(segments)
    ]

    return [
        # 開始点へ移動 → ペン下げ
        Move(x=points[0][0], y=points[0][1], v=travel_v),
        Move(z=-lift, relative=True, v=travel_v),
        # 円周をトレース + 閉じる
        *[Move(x=px, y=py, v=draw_v) for px, py in points[1:]],
        Move(x=points[0][0], y=points[0][1], v=draw_v),
        # ペン上げ
        Move(z=lift, relative=True, v=travel_v),
    ]


def send_moves(klipper: Klipper, stage: XYZStage, moves: list[Move]) -> None:
    """Moveリストを送信して完了を待つ."""
    klipper.send_gcode(stage.to_gcode(moves) + gcode.wait_for_done())


def parse_relative(line: str) -> Move:
    """入力文字列を相対Moveにパースする."""
    parts = line.split()
    if len(parts) == 2:
        return Move(x=float(parts[0]), y=float(parts[1]), relative=True)
    if len(parts) == 3:
        return Move(
            x=float(parts[0]), y=float(parts[1]), z=float(parts[2]), relative=True
        )
    msg = "入力形式: x y [z]"
    raise ValueError(msg)


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(
        description="CoreXY直行性テスト（ペン描画）",
    )
    parser.add_argument(
        "--config",
        "-c",
        type=Path,
        default=PROJECT_ROOT / "configs" / "pd_china_frame" / "machine.toml",
        help="設定ファイルのパス",
    )
    parser.add_argument(
        "--size",
        "-s",
        type=float,
        default=20.0,
        help="正方形の一辺 / 円の直径 [mm]",
    )
    parser.add_argument(
        "--lift",
        "-l",
        type=float,
        default=2.0,
        help="ペン上げ時のZ移動量 [mm]",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=10.0,
        help="描画速度 [mm/s]",
    )
    parser.add_argument(
        "--travel-speed",
        type=float,
        default=30.0,
        help="移動速度 [mm/s]",
    )
    args = parser.parse_args()

    machine = Machine(args.config)
    klipper_config = machine.klipper
    klipper = Klipper(host=klipper_config.host, port=klipper_config.port)
    stage = XYZStage(klipper.readonly)

    try:
        # ホーミング
        print("ホーミング中...")
        klipper.send_gcode(gcode.homing() + gcode.wait_for_done())
        print("ホーミング完了")

        safe_z = args.size / 2 + args.lift
        tv = args.travel_speed

        # 安全高度へ移動（描画サイズ半分 + リフト分）
        print(f"安全高度へ移動中 (Z+{safe_z}mm)...")
        send_moves(klipper, stage, [Move(z=-safe_z, relative=True, v=tv)])

        print(f"\n描画サイズ: {args.size}mm, リフト: {args.lift}mm")
        print(f"描画速度: {args.speed}mm/s, 移動速度: {args.travel_speed}mm/s")
        print("相対座標で位置を調整してください (x y [z])。'draw' で描画開始。\n")

        # 位置決めループ（相対移動）
        while True:
            line = input("> ").strip()
            if not line:
                continue
            if line.lower() == "draw":
                break

            move = parse_relative(line)
            send_moves(
                klipper,
                stage,
                [Move(x=move.x, y=move.y, z=move.z, v=tv, relative=True)],
            )
            pos = stage.get_position()
            print(f"現在位置: ({pos.x:.2f}, {pos.y:.2f}, {pos.z:.2f})")

        # 描画開始: 現在のXY位置を中心とする
        pos = stage.get_position()
        cx, cy = pos.x, pos.y

        # ペンを下ろして描画高さへ（安全高度から size/2 下降）
        send_moves(klipper, stage, [Move(z=-(args.size / 2), relative=True, v=tv)])

        # ここでペンは紙面から lift 分上にいる（描画関数の前提: ペンUP状態）
        print("正方形+対角線を描画中...")
        send_moves(
            klipper,
            stage,
            square_with_diagonals(cx, cy, args.size, args.lift, args.speed, tv),
        )

        print("円を描画中...")
        send_moves(
            klipper,
            stage,
            [
                Move(x=cx, y=cy, v=tv),
                *circle(cx, cy, args.size, args.lift, args.speed, tv),
            ],
        )

        # 安全高度に戻る
        send_moves(
            klipper,
            stage,
            [
                Move(x=cx, y=cy, v=tv),
                Move(z=args.size / 2, relative=True, v=tv),
            ],
        )

        print("描画完了")

    except KeyboardInterrupt:
        print("\n中断")
    except Exception as e:
        print(f"\nエラー: {e}")
    finally:
        klipper.send_gcode(gcode.relax())
        print("モーター脱力。終了。")


if __name__ == "__main__":
    main()
