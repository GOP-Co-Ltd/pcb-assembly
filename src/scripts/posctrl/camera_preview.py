#!/usr/bin/env python3
"""カメラのキャプチャ画像を表示する動作確認スクリプト."""

import argparse

import cv2

from pcb_assembly.hal.camera import create_camera


def main() -> None:
    parser = argparse.ArgumentParser(description="カメラのキャプチャ画像を表示")
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
    args = parser.parse_args()

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
    print("'q' で終了")

    while True:
        image = camera.capture()
        cv2.imshow("Camera Preview", image.numpy())
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
