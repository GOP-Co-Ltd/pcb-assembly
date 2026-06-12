#!/usr/bin/env python3
"""銅箔エッジ検出のパラメータ調整スクリプト.

カメラのライブ映像に検出エッジ（緑）を重ねて表示し、ウィンドウの トラックバーで Canny の low / high
を対話的に調整する。初期値は machine.toml の paste_dispenser.pad_align から読み込み、終了時に
調整後の値を設定ファイル貼り付け用に表示する。

ステージの移動はこのスクリプトでは行わない（必要なら klipper console 経由で操作する）。

操作: トラックバー (canny low / canny high) / 'm' (マスク単体表示切替) / 'q' (終了)
"""

import argparse
import logging

import cv2

from pcbasm.config import get_machine_config
from pcbasm.hal import Camera, create_camera
from pcbasm.utils import setup_logging
from pcbasm.vision import CopperEdgeDetector

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


def _run_interactive(
    camera: Camera, canny_low: int, canny_high: int, blur_ksize: int
) -> tuple[int, int]:
    """ライブ映像とトラックバーでCannyパラメータを調整し、最終値を返す."""
    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_AUTOSIZE)
    cv2.createTrackbar("canny low", WINDOW_NAME, canny_low, CANNY_MAX, lambda _: None)
    cv2.createTrackbar("canny high", WINDOW_NAME, canny_high, CANNY_MAX, lambda _: None)
    print("操作: トラックバー (canny low/high) / 'm' (マスク表示切替) / 'q' (終了)")

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

    return canny_low, canny_high


def main() -> None:
    setup_logging(logging.INFO)
    args = _parse_args()

    machine = get_machine_config(args.machine)
    pad_align = machine.paste_dispenser.pad_align

    cam_config = machine.camera
    camera = create_camera(
        device_id=cam_config.device_id,
        width=cam_config.width,
        height=cam_config.height,
        fps=cam_config.fps,
        format=cam_config.format,
        backend=cam_config.backend,
    )

    try:
        low, high = _run_interactive(
            camera,
            canny_low=round(pad_align.canny_low),
            canny_high=round(pad_align.canny_high),
            blur_ksize=pad_align.blur_ksize,
        )
    finally:
        cv2.destroyAllWindows()

    print("\n調整結果 (machine.toml の [paste_dispenser.pad_align] へ):")
    print(f"canny_low = {float(low)}")
    print(f"canny_high = {float(high)}")


if __name__ == "__main__":
    main()
