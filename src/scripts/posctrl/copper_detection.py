#!/usr/bin/env python3
"""銅箔エッジ検出のパラメータ調整スクリプト.

カメラのライブ映像に検出エッジ（緑）を重ねて表示し、ウィンドウの
トラックバーで Canny の low / high を対話的に調整する。初期値は
machine.toml の paste_dispenser.pad_align から読み込み、終了時に
調整後の値を設定ファイル貼り付け用に表示する。

操作:
- ウィンドウ: トラックバー (canny low / canny high) / 'm' (マスク単体表示切替) / 'q' (終了)
- ターミナル: move <x> <y> (機械座標へ移動) / quit (終了)
"""

import argparse
import logging
import queue
import sys
import threading

import cv2

from pcbasm import gcode
from pcbasm.config import get_machine_config
from pcbasm.hal import Camera, Klipper, Speed, XYZStage, create_camera
from pcbasm.posctrl import machine_session
from pcbasm.utils import setup_logging
from pcbasm.vision import CalibrationResult, CopperEdgeDetector

WINDOW_NAME = "Copper Detection"
CANNY_MAX = 500  # トラックバーの上限値


def _parse_args() -> argparse.Namespace:
    """CLI引数を解析する."""
    parser = argparse.ArgumentParser(description="銅箔エッジ検出のパラメータ調整")
    parser.add_argument(
        "--machine",
        "-m",
        type=str,
        default="pd_china_frame",
        help="マシン名",
    )
    return parser.parse_args()


def _read_stdin(commands: "queue.Queue[str]") -> None:
    """stdinの行をコマンドキューへ送り、EOFでquitを積む."""
    for line in sys.stdin:
        commands.put(line.strip())
    commands.put("quit")


def _print_help() -> None:
    """操作方法を表示する."""
    print("操作方法:")
    print(
        "  ウィンドウ: トラックバー (canny low/high) / 'm' (マスク表示切替) / 'q' (終了)"
    )
    print("  ターミナル: move <x> <y> (機械座標へ移動) / quit (終了)")


def _parse_move(command: str) -> tuple[float, float] | None:
    """Move コマンドから機械座標を取り出す。不正ならNone."""
    parts = command.split()
    if len(parts) != 3:
        return None
    try:
        return float(parts[1]), float(parts[2])
    except ValueError:
        return None


def _run_interactive(
    camera: Camera,
    klipper: Klipper,
    stage: XYZStage,
    canny_low: int,
    canny_high: int,
    blur_ksize: int,
) -> tuple[int, int]:
    """ライブ映像とトラックバーでCannyパラメータを調整し、最終値を返す."""
    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_AUTOSIZE)
    cv2.createTrackbar("canny low", WINDOW_NAME, canny_low, CANNY_MAX, lambda _: None)
    cv2.createTrackbar("canny high", WINDOW_NAME, canny_high, CANNY_MAX, lambda _: None)

    commands: queue.Queue[str] = queue.Queue()
    threading.Thread(target=_read_stdin, args=(commands,), daemon=True).start()
    _print_help()

    detector = CopperEdgeDetector(
        canny_low=canny_low, canny_high=canny_high, blur_ksize=blur_ksize
    )
    show_mask = False
    while True:
        low = cv2.getTrackbarPos("canny low", WINDOW_NAME)
        high = cv2.getTrackbarPos("canny high", WINDOW_NAME)
        if (low, high) != (canny_low, canny_high):
            canny_low, canny_high = low, high
            detector = CopperEdgeDetector(
                canny_low=canny_low, canny_high=canny_high, blur_ksize=blur_ksize
            )

        image = camera.capture()
        edges = detector.detect_edges(image)
        if show_mask:
            display = cv2.cvtColor(edges, cv2.COLOR_GRAY2BGR)
        else:
            display = image.numpy().copy()
            display[edges > 0] = (0, 255, 0)

        line = f"canny: {canny_low} / {canny_high}  (m: mask, q: quit)"
        cv2.putText(
            display, line, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 3
        )
        cv2.putText(
            display, line, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1
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
        if command.startswith("move"):
            target = _parse_move(command)
            if target is None:
                print("使い方: move <x> <y>")
                continue
            print(f"移動: Machine({target[0]:.3f}, {target[1]:.3f})")
            klipper.send_gcode(
                stage.move(x=target[0], y=target[1], speed=Speed.absolute(30))
                + gcode.wait_for_done()
            )
        elif command:
            print(f"不明なコマンド: {command}")
            _print_help()

    return canny_low, canny_high


def main() -> None:
    setup_logging(logging.INFO)
    args = _parse_args()

    machine = get_machine_config(args.machine)
    pad_align = machine.paste_dispenser.pad_align

    klipper = Klipper(host=machine.klipper.host, port=machine.klipper.port)
    stage = XYZStage(klipper.readonly)

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

    print("=== ホーミング (G28) ===")
    klipper.send_gcode(gcode.homing(x=True, y=True, z=True) + gcode.wait_for_done())
    if calibration.z_position is not None:
        klipper.send_gcode(stage.move(z=calibration.z_position) + gcode.wait_for_done())

    with machine_session(klipper):
        low, high = _run_interactive(
            camera,
            klipper,
            stage,
            canny_low=round(pad_align.canny_low),
            canny_high=round(pad_align.canny_high),
            blur_ksize=pad_align.blur_ksize,
        )

    print("\n調整結果 (machine.toml の [paste_dispenser.pad_align] へ):")
    print(f"canny_low = {float(low)}")
    print(f"canny_high = {float(high)}")


if __name__ == "__main__":
    main()
