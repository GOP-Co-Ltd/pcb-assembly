"""Board巡回用のカメラ表示ユーティリティ."""

import cv2

from pcbasm import gcode
from pcbasm.geometry import Move, Point2d
from pcbasm.hal import Camera
from pcbasm.posctrl.setup import BoardCalibrationResult
from pcbasm.vision import draw_overlay


def display_at_point(
    result: BoardCalibrationResult,
    machine_pt: Point2d,
    label: str,
    duration: float = 0.5,
    window_name: str = "Board Tour",
):
    """指定座標へ移動し、ラベル付きカメラ映像を一定時間表示する."""
    result.klipper.send_gcode(
        result.stage.to_gcode(Move(x=machine_pt.x, y=machine_pt.y, v=30))
        + gcode.wait_for_done()
    )

    crop_size = result.machine.camera.crop.size
    camera = result.camera
    for _ in range(int(camera.resolution.fps * duration)):
        frame = camera.capture()
        img = draw_overlay(frame, crop_size).numpy()
        cv2.putText(img, label, (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        cv2.imshow(window_name, img)
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
        result.stage.to_gcode(Move(x=machine_pt.x, y=machine_pt.y, v=30))
        + gcode.wait_for_done()
    )

    crop_size = result.machine.camera.crop.size
    camera = result.camera
    while True:
        frame = camera.capture()
        img = draw_overlay(frame, crop_size).numpy()
        cv2.putText(img, label, (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        cv2.imshow(window_name, img)
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
