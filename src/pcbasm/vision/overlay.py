"""画像オーバーレイ描画ユーティリティ."""

import cv2

from pcbasm.geometry import Point2d
from pcbasm.vision.detection import DetectedCircle
from pcbasm.vision.image import Image, ImageArray


def draw_detected_circle(
    img: ImageArray, circle: DetectedCircle, crop_size: tuple[int, int]
) -> None:
    """検出円・その中心・カメラ中心と結ぶ線を描画する（in-place）.

    circle.center はクロップ座標系（原点はクロップ左上）なので、 フル画像座標へ変換する。
    """
    h, w = img.shape[:2]
    cx, cy = w // 2, h // 2
    half_w, half_h = crop_size[0] // 2, crop_size[1] // 2

    circle_x = int(cx - half_w + circle.center.x)
    circle_y = int(cy - half_h + circle.center.y)
    radius = int(circle.radius)

    cv2.circle(img, (circle_x, circle_y), radius, (0, 0, 255), 2)
    cv2.circle(img, (circle_x, circle_y), 3, (0, 0, 255), -1)
    cv2.line(img, (cx, cy), (circle_x, circle_y), (255, 0, 0), 2)


def draw_crosshair(img: ImageArray) -> None:
    """画像中心に十字線を描画する（in-place）."""
    h, w = img.shape[:2]
    cx, cy = w // 2, h // 2
    color = (0, 255, 0)
    cv2.line(img, (cx - 30, cy), (cx + 30, cy), color, 1)
    cv2.line(img, (cx, cy - 30), (cx, cy + 30), color, 1)


def draw_overlay(
    image: Image,
    crop_size: tuple[int, int],
    offset: Point2d | None = None,
) -> Image:
    """画像に十字線、関心領域、オフセット情報を描画する."""
    img = image.numpy().copy()
    h, w = img.shape[:2]
    cx, cy = w // 2, h // 2

    color = (0, 255, 0)

    draw_crosshair(img)

    half_w, half_h = crop_size[0] // 2, crop_size[1] // 2
    x1, y1 = cx - half_w, cy - half_h
    x2, y2 = cx + half_w, cy + half_h
    cv2.rectangle(img, (x1, y1), (x2, y2), color, 1)

    if offset is not None:
        text = f"Offset: ({offset.x:.3f}, {offset.y:.3f}) mm"
        cv2.putText(img, text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
        dist_text = f"Distance: {offset.norm:.3f} mm"
        cv2.putText(img, dist_text, (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)

    return Image(img)
