"""Board巡回用のカメラ表示ユーティリティ（cv2 ウィンドウ専用）."""

import cv2

from pcbasm import gcode
from pcbasm.geometry import Point2d
from pcbasm.hal import Camera, Speed
from pcbasm.posctrl.render import render_label
from pcbasm.posctrl.setup import BoardCalibrationResult
from pcbasm.vision import FrameSink, Image, draw_overlay


def window_sink(window_name: str) -> FrameSink:
    """フレームを cv2 ウィンドウへ表示する FrameSink を返す.

    Args:
        window_name: 表示先ウィンドウ名（cv2.imshow が自動生成する）
    """

    def sink(image: Image) -> None:
        cv2.imshow(window_name, image.numpy())
        cv2.waitKey(1)

    return sink


def display_at_point(
    result: BoardCalibrationResult,
    machine_pt: Point2d,
    label: str,
    duration: float = 0.5,
    window_name: str = "Board Tour",
):
    """指定座標へ移動し、ラベル付きカメラ映像を一定時間表示する."""
    result.klipper.send_gcode(
        result.stage.move(x=machine_pt.x, y=machine_pt.y, speed=Speed.absolute(30))
        + gcode.wait_for_done()
    )

    crop_size = result.machine.camera.crop.size
    camera = result.camera
    for _ in range(int(camera.resolution.fps * duration)):
        frame = render_label(camera.capture(), crop_size, label)
        cv2.imshow(window_name, frame.numpy())
        if cv2.waitKey(1) == 27:  # Esc
            print("中断しました")
            break


def interactive_display_at_point(
    result: BoardCalibrationResult,
    machine_pt: Point2d,
    label: str,
    window_name: str = "Board Tour",
):
    """指定座標へ移動し、キーが押されるまでラベル付きカメラ映像を表示する."""
    result.klipper.send_gcode(
        result.stage.move(x=machine_pt.x, y=machine_pt.y, speed=Speed.absolute(30))
        + gcode.wait_for_done()
    )

    crop_size = result.machine.camera.crop.size
    camera = result.camera
    while True:
        frame = render_label(camera.capture(), crop_size, label)
        cv2.imshow(window_name, frame.numpy())
        if cv2.waitKey(100) != -1:
            break


def wait_for_keypress(
    camera: Camera,
    crop_size: tuple[int, int],
    window_name: str = "Board Tour",
):
    """何かキーが押されるまでカメラ映像を表示し続ける."""
    print("\n何かキーを押すと終了します...")
    while True:
        frame = camera.capture()
        display = draw_overlay(frame, crop_size)
        cv2.imshow(window_name, display.numpy())
        if cv2.waitKey(100) != -1:
            break
