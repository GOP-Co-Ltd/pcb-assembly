#!/usr/bin/env python3
"""銅箔エッジ検出の動作確認スクリプト.

ライブカメラまたは静止画 (--image) に検出エッジを重畳表示する。 エッジ検出のハイパーパラメータを CLI
引数で調整でき、現場での閾値合わせに使う。 'm' でマスク単体表示に切替、'q' で終了。
"""

import argparse
from collections.abc import Iterator
from pathlib import Path

import cv2

from pcbasm.hal.camera import create_camera
from pcbasm.vision import CopperEdgeDetector, Image

WINDOW_NAME = "Copper Edge Detection"


def _frames(args: argparse.Namespace) -> Iterator[Image]:
    """入力源 (静止画 or ライブカメラ) から画像を返し続ける."""
    if args.image is not None:
        image = Image.load(Path(args.image))
        print(f"静止画: {args.image}")
        while True:
            yield image
    else:
        camera = create_camera(
            device_id=args.device,
            width=args.width,
            height=args.height,
            fps=args.fps,
            format=args.format,
            backend=args.backend,
        )
        print(f"カメラ: {camera.info.name}")
        print(f"解像度: {args.width}x{args.height} @ {args.fps}fps")
        while True:
            yield camera.capture()


def main() -> None:
    parser = argparse.ArgumentParser(description="銅箔エッジの検出結果を表示")
    parser.add_argument("--device", "-d", type=int, default=0, help="カメラデバイスID")
    parser.add_argument("--width", "-W", type=int, default=1280, help="幅")
    parser.add_argument("--height", "-H", type=int, default=720, help="高さ")
    parser.add_argument("--fps", "-f", type=float, default=30.0, help="FPS")
    parser.add_argument(
        "--format", "-F", type=str, default=None, help="フォーマット (例: MJPG)"
    )
    parser.add_argument(
        "--backend", "-b", type=str, default="csi", help="バックエンド (usb/csi)"
    )
    parser.add_argument(
        "--image",
        "-i",
        type=str,
        default=None,
        help="静止画パス (指定時はカメラ不使用)",
    )
    parser.add_argument("--canny-low", type=float, default=50.0, help="Canny下側閾値")
    parser.add_argument("--canny-high", type=float, default=150.0, help="Canny上側閾値")
    parser.add_argument(
        "--blur-ksize", type=int, default=5, help="GaussianBlurカーネルサイズ (奇数)"
    )
    args = parser.parse_args()

    detector = CopperEdgeDetector(
        canny_low=args.canny_low,
        canny_high=args.canny_high,
        blur_ksize=args.blur_ksize,
    )

    print("'m' でマスク単体表示切替、'q' で終了")

    show_mask = False
    for image in _frames(args):
        mask = detector.detect_edges(image)
        if show_mask:
            display = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
        else:
            display = image.numpy().copy()
            display[mask > 0] = (0, 255, 0)

        cv2.imshow(WINDOW_NAME, display)
        key = cv2.waitKey(50 if args.image else 1) & 0xFF
        if key == ord("m"):
            show_mask = not show_mask
        elif key == ord("q"):
            break

    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
