"""Board 座標の点の周辺を camera frame から固定ピクセル寸法で切り抜く."""

from __future__ import annotations

import math

import attrs
import numpy as np

from pcbasm.geometry import Point2d
from pcbasm.utils import is_finite_number
from pcbasm.vision.image import Image, ImageArray, PixelRect


@attrs.frozen
class RectCrop:
    """固定ピクセル寸法の RGB crop と全 frame 上の crop 矩形."""

    image: ImageArray = attrs.field(eq=False)
    pixel_rect: PixelRect


def crop_pixel_size(
    size_mm: float, pixel_per_mm: float
) -> tuple[int | None, str | None]:
    """Crop 寸法と物理スケールから 1 回だけ整数ピクセル寸法を決める.

    収集の開始時に 1 回だけ呼び、以降の全 crop はこの寸法を使う（crop ごとに floor / ceil
    しないことで全画像が同一ピクセル寸法になる）。

    中心 pixel が 1 つ存在するよう奇数へ寄せる。
    """
    for name, value in (("size_mm", size_mm), ("pixel_per_mm", pixel_per_mm)):
        if not is_finite_number(value) or value <= 0:
            return None, f"{name}は正の有限値が必要です: {value!r}"
    size = int(round(float(size_mm) * float(pixel_per_mm)))
    if size < 1:
        return None, (
            f"crop寸法が1 pixel未満です: {size_mm!r} mm x {pixel_per_mm!r} px/mm"
        )
    return size if size % 2 == 1 else size + 1, None


def crop_centered(
    image: Image | ImageArray,
    center: Point2d,
    matrix: ImageArray,
    shift: ImageArray,
    *,
    pixel_size: int,
) -> tuple[RectCrop | None, str | None]:
    """Board 座標の中心を board→pixel affine で射影し、その周りを固定寸法で切り出す.

    ``matrix`` / ``shift`` は
    :meth:`pcbasm.posctrl.CopperProjector.board_to_pixel_affine` の戻り値をそのまま
    受け取る。crop が frame 外へ出る場合は padding せず ``(None, 理由)`` を返す。
    """
    if type(pixel_size) is not int or pixel_size < 1:
        return None, f"pixel_sizeは1以上の整数が必要です: {pixel_size!r}"

    source = image.numpy() if isinstance(image, Image) else np.asarray(image)
    if source.ndim != 3 or source.shape[2] != 3:
        return None, f"RGB画像は3 channelが必要です: shape={source.shape}"
    affine = np.asarray(matrix, dtype=np.float64)
    translation = np.asarray(shift, dtype=np.float64)
    if affine.shape != (2, 2) or translation.shape != (2,):
        return None, "board→pixel affineはmatrix=(2, 2), shift=(2,)が必要です"
    if not np.isfinite(affine).all() or not np.isfinite(translation).all():
        return None, "board→pixel affineには有限値が必要です"

    projected = np.asarray([center.x, center.y], dtype=np.float64) @ affine.T
    projected = projected + translation
    center_x = float(projected[0])
    center_y = float(projected[1])
    if not math.isfinite(center_x) or not math.isfinite(center_y):
        return None, f"crop中心の射影が有限値になりません: {center!r}"
    x0 = int(round(center_x - pixel_size / 2.0))
    y0 = int(round(center_y - pixel_size / 2.0))
    x1 = x0 + pixel_size
    y1 = y0 + pixel_size
    height, width = source.shape[:2]
    if x0 < 0 or y0 < 0 or x1 > width or y1 > height:
        return None, (
            f"crop {x0, y0, x1, y1} がcamera frame {width, height} に収まりません"
        )
    return RectCrop(
        image=source[y0:y1, x0:x1].copy(), pixel_rect=(x0, y0, x1, y1)
    ), None
