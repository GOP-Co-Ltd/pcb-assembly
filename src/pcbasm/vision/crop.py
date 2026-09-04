"""Board 座標 polygon の周辺を camera frame から切り抜く（crop + mask）."""

from __future__ import annotations

import math

import attrs
import cv2
import numpy as np
from shapely import Polygon
from shapely.coords import CoordinateSequence

from pcbasm.utils import is_finite_number
from pcbasm.vision.image import Image, ImageArray, PixelRect


@attrs.frozen
class PolygonCrop:
    """Polygon 周辺の RGB 画像、同寸法 mask、全 frame 上の crop 矩形."""

    image: ImageArray = attrs.field(eq=False)
    mask: ImageArray = attrs.field(eq=False)
    pixel_rect: PixelRect


def validate_crop_margins(crop_margin_mm: float, mask_margin_mm: float) -> str | None:
    """画像 crop と polygon mask の余白設定を検証する."""
    for name, value in (
        ("crop_margin_mm", crop_margin_mm),
        ("mask_margin_mm", mask_margin_mm),
    ):
        if not is_finite_number(value) or value < 0:
            return f"{name}は0以上の有限値が必要です: {value!r}"
    if mask_margin_mm > crop_margin_mm:
        return "mask_margin_mmはcrop_margin_mm以下にしてください"
    return None


def crop_polygon(
    image: Image | ImageArray,
    polygon: Polygon,
    matrix: ImageArray,
    shift: ImageArray,
    *,
    margin_mm: float,
    mask_margin_mm: float,
) -> tuple[PolygonCrop | None, str | None]:
    """Polygon の AABB に margin を足し、buffer 付き mask と RGB crop を返す.

    ``matrix`` / ``shift`` は
    :meth:`pcbasm.posctrl.CopperProjector.board_to_pixel_affine` の戻り値を
    そのまま受け取る。crop が frame 外へ出る場合は padding せず ``(None, 理由)`` を返す。
    """
    margin_error = validate_crop_margins(margin_mm, mask_margin_mm)
    if margin_error is not None:
        return None, margin_error
    if polygon.is_empty or not polygon.is_valid:
        return None, "crop対象polygonが空または不正です"

    source = image.numpy() if isinstance(image, Image) else np.asarray(image)
    if source.ndim != 3 or source.shape[2] != 3:
        return None, f"RGB画像は3 channelが必要です: shape={source.shape}"
    affine = np.asarray(matrix, dtype=np.float64)
    translation = np.asarray(shift, dtype=np.float64)
    if affine.shape != (2, 2) or translation.shape != (2,):
        return None, "board→pixel affineはmatrix=(2, 2), shift=(2,)が必要です"
    if not np.isfinite(affine).all() or not np.isfinite(translation).all():
        return None, "board→pixel affineには有限値が必要です"

    min_x, min_y, max_x, max_y = polygon.bounds
    bounds = np.asarray(
        [
            [min_x - margin_mm, min_y - margin_mm],
            [max_x + margin_mm, min_y - margin_mm],
            [max_x + margin_mm, max_y + margin_mm],
            [min_x - margin_mm, max_y + margin_mm],
        ],
        dtype=np.float64,
    )
    projected = bounds @ affine.T + translation
    x0 = math.floor(float(projected[:, 0].min()))
    y0 = math.floor(float(projected[:, 1].min()))
    x1 = math.ceil(float(projected[:, 0].max()))
    y1 = math.ceil(float(projected[:, 1].max()))
    height, width = source.shape[:2]
    if x0 < 0 or y0 < 0 or x1 > width or y1 > height:
        return None, (
            f"polygon crop {x0, y0, x1, y1} がcamera frame "
            f"{width, height} に収まりません"
        )
    if x0 >= x1 or y0 >= y1:
        return None, f"polygon cropが空です: {x0, y0, x1, y1}"

    crop = source[y0:y1, x0:x1].copy()
    mask = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
    mask_polygon = polygon if mask_margin_mm == 0 else polygon.buffer(mask_margin_mm)
    if not isinstance(mask_polygon, Polygon):
        return None, "buffer後のmask polygonが不正です"
    exterior = _ring_pixels(mask_polygon.exterior.coords, affine, translation, (x0, y0))
    cv2.fillPoly(mask, [exterior], 255)
    holes = [
        _ring_pixels(ring.coords, affine, translation, (x0, y0))
        for ring in mask_polygon.interiors
    ]
    if holes:
        cv2.fillPoly(mask, holes, 0)
    return PolygonCrop(image=crop, mask=mask, pixel_rect=(x0, y0, x1, y1)), None


def _ring_pixels(
    coordinates: CoordinateSequence,
    matrix: ImageArray,
    shift: ImageArray,
    origin: tuple[int, int],
) -> ImageArray:
    points = np.asarray(coordinates, dtype=np.float64)
    pixels = points @ matrix.T + shift - np.asarray(origin, dtype=np.float64)
    return np.round(pixels).astype(np.int32).reshape(-1, 1, 2)
