#!/usr/bin/env python3
"""設計銅箔と観測エッジの照合による基板位置合わせデモスクリプト.

boardキャリブレーション後、現在位置で見えるはずの設計銅箔 (TOP layer) を
カメラ画像へ半透明overlayし、観測エッジとの位置ずれ dx,dy を表示する。

操作系は二重化されている:
- ターミナル: adjust (1回補正) / move <x> <y> (board座標へ移動) / quit (終了)
- ウィンドウ: 'm' (マスク単体表示切替) / 'q' (終了)
"""

import argparse
import logging
import queue
import sys
import threading
from pathlib import Path

import cv2
import numpy as np

from pcbasm import gcode
from pcbasm.config import get_machine_config
from pcbasm.geometry import Point2d
from pcbasm.hal import Speed
from pcbasm.pcb import Layer
from pcbasm.posctrl import (
    BoardCalibrationResult,
    CopperEdgeMatcher,
    CopperProjection,
    CopperProjector,
    EdgeMatch,
    machine_session,
    setup_board_calibration,
)
from pcbasm.utils import setup_logging
from pcbasm.vision import CopperEdgeDetector, Image, ImageArray

WINDOW_NAME = "Copper Detection"
FILL_COLOR = (0, 0, 255)  # 想定銅箔overlayの色 (BGR)
FILL_ALPHA = 0.35


def _parse_args() -> argparse.Namespace:
    """CLI引数を解析する."""
    parser = argparse.ArgumentParser(description="設計銅箔との照合による位置合わせデモ")
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
    parser.add_argument("--canny-low", type=float, default=50.0, help="Canny下側閾値")
    parser.add_argument("--canny-high", type=float, default=150.0, help="Canny上側閾値")
    parser.add_argument(
        "--blur-ksize", type=int, default=5, help="GaussianBlurカーネルサイズ (奇数)"
    )
    parser.add_argument(
        "--search-window", type=float, default=2.0, help="照合の探索窓 片側幅 (mm)"
    )
    return parser.parse_args()


def _read_stdin(commands: "queue.Queue[str]") -> None:
    """stdinの行をコマンドキューへ送り、EOFでquitを積む."""
    for line in sys.stdin:
        commands.put(line.strip())
    commands.put("quit")


def _print_help() -> None:
    """操作方法を表示する."""
    print("操作方法 (二重化):")
    print("  ターミナル: adjust (1回補正) / move <x> <y> (board座標へ移動) / quit")
    print("  ウィンドウ: 'm' (マスク単体表示切替) / 'q' (終了)")


def _parse_move(command: str) -> Point2d | None:
    """Move コマンドからboard座標を取り出す。不正ならNone."""
    parts = command.split()
    if len(parts) != 3:
        return None
    try:
        return Point2d(x=float(parts[1]), y=float(parts[2]))
    except ValueError:
        return None


def _status_lines(match: EdgeMatch | None, board_pos: Point2d) -> list[str]:
    """画面表示するステータス行を組み立てる."""
    lines = [f"board: ({board_pos.x:.2f}, {board_pos.y:.2f}) mm"]
    if match is None:
        lines.append("match: N/A")
    else:
        px = match.offset.px
        mm = match.offset.mm
        lines.append(
            f"dx,dy: ({px.x:+.0f}, {px.y:+.0f}) px / ({mm.x:+.3f}, {mm.y:+.3f}) mm"
        )
        lines.append(f"mean distance: {match.mean_distance_px:.2f} px")
    lines.append("adjust | move <x> <y> | quit (terminal), m/q (window)")
    return lines


def _render(
    image: Image,
    edges: ImageArray,
    projection: CopperProjection,
    show_mask: bool,
    lines: list[str],
) -> ImageArray:
    """検出エッジ・想定銅箔overlay・ステータスを描画した画像を返す."""
    if show_mask:
        display = cv2.cvtColor(edges, cv2.COLOR_GRAY2BGR)
    else:
        display = image.numpy().copy()
        display[edges > 0] = (0, 255, 0)

    color_layer = np.zeros_like(display)
    color_layer[:] = FILL_COLOR
    blended = cv2.addWeighted(display, 1.0 - FILL_ALPHA, color_layer, FILL_ALPHA, 0.0)
    fill = projection.fill_mask > 0
    display[fill] = blended[fill]

    for i, line in enumerate(lines):
        position = (10, 25 + i * 25)
        cv2.putText(
            display, line, position, cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3
        )
        cv2.putText(
            display, line, position, cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1
        )
    return display


def _move_to(result: BoardCalibrationResult, target: Point2d) -> None:
    """ステージを機械座標targetへ移動する."""
    result.klipper.send_gcode(
        result.stage.move(x=target.x, y=target.y, speed=Speed.absolute(30))
        + gcode.wait_for_done()
    )


def _adjust(result: BoardCalibrationResult, match: EdgeMatch | None) -> None:
    """直近の照合結果で1回だけ位置補正する。anchorは更新しない."""
    if match is None:
        print("警告: 有効な照合結果がないため補正をスキップ")
        return
    pos = result.stage.get_position().to2d()
    target = pos - result.offset_transform.apply(match.offset.mm)
    print(f"補正移動: ({pos.x:.4f}, {pos.y:.4f}) -> ({target.x:.4f}, {target.y:.4f})")
    _move_to(result, target)


def _run_interactive(result: BoardCalibrationResult, args: argparse.Namespace) -> None:
    """ライブ映像と並行コマンド入力で照合・補正を行う."""
    detector = CopperEdgeDetector(
        canny_low=args.canny_low,
        canny_high=args.canny_high,
        blur_ksize=args.blur_ksize,
    )
    polygons = [c.polygon for c in result.pcb.copper if c.layer == Layer.TOP]
    print(f"TOPレイヤーの銅箔島数: {len(polygons)}")

    frame = result.camera.capture()
    projector = CopperProjector(
        polygons=polygons,
        board_transform=result.board_transform,
        offset_transform=result.offset_transform,
        pixel_per_mm=result.calibration.pixel_per_mm,
        image_size=frame.size,
    )
    matcher = CopperEdgeMatcher(
        pixel_per_mm=result.calibration.pixel_per_mm,
        search_window_mm=args.search_window,
        crop_size=result.machine.camera.crop.size,
    )

    # 投影anchorは指令位置に固定する（adjustでは更新しない）。
    # live位置で毎フレーム投影すると観測と投影が同量動き、補正が収束しない
    anchor = result.stage.get_position().to2d()
    board_pos = result.board_transform.inverse().apply(anchor)
    projection = projector.project(anchor)

    commands: queue.Queue[str] = queue.Queue()
    threading.Thread(target=_read_stdin, args=(commands,), daemon=True).start()
    _print_help()

    show_mask = False
    last_match: EdgeMatch | None = None
    while True:
        image = result.camera.capture()
        edges = detector.detect_edges(image)
        match = matcher.match(edges, projection.edge_mask)
        if match is not None:
            last_match = match

        display = _render(
            image, edges, projection, show_mask, _status_lines(last_match, board_pos)
        )
        cv2.imshow(WINDOW_NAME, display)
        key = cv2.waitKey(1) & 0xFF
        if key == ord("m"):
            show_mask = not show_mask
        elif key == ord("q"):
            break

        try:
            command = commands.get_nowait()
        except queue.Empty:
            continue
        if command == "quit":
            break
        if command == "adjust":
            _adjust(result, last_match)
        elif command.startswith("move"):
            target = _parse_move(command)
            if target is None:
                print("使い方: move <x> <y>")
                continue
            anchor = result.board_transform.apply(target)
            print(
                f"移動: Board({target.x:.2f}, {target.y:.2f}) -> "
                f"Machine({anchor.x:.3f}, {anchor.y:.3f})"
            )
            _move_to(result, anchor)
            board_pos = target
            projection = projector.project(anchor)
        elif command:
            print(f"不明なコマンド: {command}")
            _print_help()


def main() -> None:
    setup_logging(logging.INFO)
    args = _parse_args()

    machine = get_machine_config(args.machine)
    result = setup_board_calibration(
        machine=machine,
        pcb_file_path=args.pcb_file,
        tolerance=args.tolerance,
        window_name=WINDOW_NAME,
    )

    with machine_session(result.klipper):
        _run_interactive(result, args)


if __name__ == "__main__":
    main()
