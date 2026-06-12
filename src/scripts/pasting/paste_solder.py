#!/usr/bin/env python3
"""ペースト塗布の実機運用スクリプト.

ボード計測ベースのフロー:
1. setup_board_calibration() で初期化〜Board変換計測、PasteSession.from_calibration() で塗布用HAL構築
2. PadAlignmentSession で部品単位の銅箔照合による位置合わせ
3. HeightPlaneMeasurer.measure() でその場で高さ計測
4. TOPレイヤーのパッドを取得・補正適用・ソート
5. PasteApplicator作成
6. 任意で対話的ローディング → リトラクション → パッド中心にポイント塗布
"""

import argparse
import logging
from pathlib import Path

from shapely import Polygon

from pcbasm import gcode
from pcbasm.config import get_machine_config
from pcbasm.geometry import (
    Compose,
    HeightPlane,
    Point2d,
    sort_by_nearest,
    transform_polygon,
)
from pcbasm.pasting import interactive_loading
from pcbasm.pcb import Copper, Layer, Pad
from pcbasm.posctrl import (
    BoardCalibrationResult,
    ComponentAlignments,
    ComponentPads,
    PadAlignmentResult,
    PadAlignmentSession,
    setup_board_calibration,
    sorted_top_component_pads,
)
from pcbasm.session import PasteSession
from pcbasm.utils import setup_logging

WINDOW_NAME = "Paste Solder"


def _align_components(result: BoardCalibrationResult) -> ComponentAlignments:
    """TOP層の部品を銅箔照合で位置合わせし、結果のlookupを返す."""
    print("\n=== Pad位置合わせ ===")
    # padをdesignatorで部品へ対応付け、部品座標でnearest neighborソート
    sorted_groups = sorted_top_component_pads(result)
    print(f"padを持つ部品数: {len(sorted_groups)}")

    session = PadAlignmentSession.from_calibration(result, window_name=WINDOW_NAME)
    results: list[tuple[ComponentPads, PadAlignmentResult]] = []
    for i, group in enumerate(sorted_groups):
        progress = f"{group.component.designator} {i + 1}/{len(sorted_groups)}"
        alignment = session.align(group)
        if alignment is None:
            print(f"警告: {progress} の照合に失敗")
            continue
        translation = alignment.translation
        print(
            f"  {progress}: "
            f"dx={translation.x:+.4f} dy={translation.y:+.4f} mm, "
            f"theta={alignment.rotation.degrees:+.3f} deg"
        )
        results.append((group, alignment))

    aligned_pads = sum(len(group.pads) for group, _ in results)
    print(
        f"位置合わせ成功: {len(results)}/{len(sorted_groups)} 部品 ({aligned_pads} pads)"
    )
    return ComponentAlignments(
        board_transform=result.board_transform, results=tuple(results)
    )


def _measure_height(session: PasteSession, top_coppers: list[Copper]) -> HeightPlane:
    """height_measurer.measure() を実行して HeightPlane を返す."""
    print("\n=== Height plane計測 ===")
    return session.height_measurer.measure(
        coppers=top_coppers,
        board_to_machine=session.board_to_machine,
        outline=session.pcb.outline.polygon,
    )


def _corrected_polygons(
    top_pads: list[Pad], alignments: ComponentAlignments
) -> list[Polygon]:
    """padごとに所属部品の補正をboard座標で適用した塗布ポリゴンを作る."""
    polygons: list[Polygon] = []
    for pad in top_pads:
        correction = alignments.board_correction(pad.designator)
        if correction is None:
            print(
                f"警告: {pad.designator}.{pad.pad_number} は未照合のため無補正で塗布します"
            )
            polygons.append(pad.polygon)
        else:
            polygons.append(transform_polygon(pad.polygon, correction))
    return polygons


def _load_and_apply(
    session: PasteSession,
    height_plane: HeightPlane,
    top_pads: list[Pad],
    alignments: ComponentAlignments,
    args: argparse.Namespace,
) -> None:
    """補正適用 → Nearest ソート → PasteApplicator 構築 → 任意ローディング → リトラクション → 塗布."""
    stage = session.stage

    # 位置合わせ結果をboard座標の補正として塗布ポリゴンへ適用
    polygons = _corrected_polygons(top_pads, alignments)

    # 補正済みポリゴン重心でnearest-neighborソート
    current_pos = stage.get_position()
    sorted_polygons = sort_by_nearest(
        polygons,
        current_pos.to2d().to3d(),
        key=lambda p: Point2d(x=p.centroid.x, y=p.centroid.y).to3d(),
    )

    # board→machine全変換 (board_transform + toolhead_offset + height_plane)
    transform = Compose(
        [session.board_transform, session.toolhead_offset, height_plane]
    )

    # PasteApplicator作成
    with session.make_applicator(transform=transform) as applicator:
        # 対話的ローディング
        if args.interactive_loading:
            pos = stage.get_position()
            session.klipper.send_gcode(stage.move(x=0, y=0, z=0))
            interactive_loading(applicator, args.amount)
            session.klipper.send_gcode(
                stage.move(x=pos.x, y=pos.y, z=pos.z) + gcode.wait_for_done()
            )

        # リトラクション
        print("\n=== リトラクション ===")
        applicator.retract()
        print("リトラクション完了")

        # パッド中心にポイント塗布
        print(f"\n=== パッド塗布 ({len(sorted_polygons)} パッド) ===")
        applicator.apply(sorted_polygons)
        print("塗布完了")


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(description="ペースト塗布の実機運用スクリプト")
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
        "--amount",
        type=float,
        default=0.1,
        help="ローディング時のデフォルト押し出し量 [uL]",
    )
    parser.add_argument(
        "--interactive-loading",
        "-l",
        action="store_true",
        help="指定時のみ対話的ローディングを実行する",
    )
    args = parser.parse_args()

    machine = get_machine_config(args.machine)
    result = setup_board_calibration(
        machine=machine,
        pcb_file_path=args.pcb_file,
        tolerance=args.tolerance,
        window_name=WINDOW_NAME,
    )
    session = PasteSession.from_calibration(result)

    with session:
        # TOPレイヤーの銅箔・パッドを取得
        top_coppers = [c for c in session.pcb.copper if c.layer == Layer.TOP]
        top_pads = [p for p in session.pcb.pads if p.layer == Layer.TOP]

        try:
            alignments = _align_components(result)
            height_plane = _measure_height(session, top_coppers)
            _load_and_apply(session, height_plane, top_pads, alignments, args)
        except KeyboardInterrupt:
            print("\n=== 中止 ===")
            session.klipper.send_gcode(gcode.relax())


if __name__ == "__main__":
    main()
