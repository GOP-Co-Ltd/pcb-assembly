"""画像オーバーレイ描画ユーティリティ."""

from collections.abc import Sequence

import cv2
import numpy as np

from pcbasm.geometry import Point2d
from pcbasm.vision.calibration import CheckerboardView
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


def draw_scan_coverage(
    image_size: tuple[int, int],
    views: Sequence[CheckerboardView],
    crop_sizes: Sequence[tuple[int, int]] = (),
) -> Image:
    """全視点のコーナーを 1 枚に散布し、関心領域の候補矩形を重ねる.

    校正データがどの半径まで届いているかを目で確認するための図。矩形は画像中心
    基準なので、crop を広げたときに校正済みの領域から出ないかを判定できる。

    Args:
        image_size: 出力画像のサイズ (width, height)。視点の元フレームと同じもの
        views: 散布するコーナーを持つ視点
        crop_sizes: 重ねる矩形のサイズ (width, height) の列

    Returns:
        黒背景にコーナー・十字線・矩形を描いた画像
    """
    width, height = image_size
    img = np.zeros((height, width, 3), dtype=np.uint8)
    cx, cy = width // 2, height // 2

    for view in views:
        for point in np.asarray(view.corners, dtype=np.float64).reshape(-1, 2):
            cv2.circle(img, (round(point[0]), round(point[1])), 1, (0, 255, 0), -1)

    draw_crosshair(img)

    for crop_width, crop_height in crop_sizes:
        half_w, half_h = crop_width // 2, crop_height // 2
        color = (0, 255, 255)
        cv2.rectangle(
            img, (cx - half_w, cy - half_h), (cx + half_w, cy + half_h), color, 1
        )
        cv2.putText(
            img,
            f"{crop_width}x{crop_height}",
            (cx - half_w + 4, cy - half_h - 6),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            color,
            1,
        )

    return Image(img)
