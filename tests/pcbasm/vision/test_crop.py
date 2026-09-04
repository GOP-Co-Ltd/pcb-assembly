"""Polygon 周辺 crop / mask 生成（vision.crop）の公開契約."""

import numpy as np
import pytest
from shapely import Polygon, box

from pcbasm.vision import Image
from pcbasm.vision.crop import crop_polygon, validate_crop_margins


def _source_image(width: int = 120, height: int = 100) -> np.ndarray:
    """切り抜き位置とRGB保持を同時に確認できる合成画像."""
    yy, xx = np.indices((height, width), dtype=np.uint8)
    return np.dstack((xx, yy, xx ^ yy))


class TestValidateCropMargins:
    def test_accepts_mask_margin_up_to_crop_margin(self):
        assert validate_crop_margins(1.0, 1.0) is None
        assert validate_crop_margins(1.0, 0.0) is None

    @pytest.mark.parametrize(
        ("crop_margin_mm", "mask_margin_mm", "expected"),
        [
            (-0.01, 0.0, "crop_margin_mm"),
            (1.0, -0.01, "mask_margin_mm"),
            (float("nan"), 0.0, "crop_margin_mm"),
            (0.1, 0.2, "mask_margin_mm"),
        ],
    )
    def test_rejects_invalid_margins(
        self, crop_margin_mm: float, mask_margin_mm: float, expected: str
    ):
        error = validate_crop_margins(crop_margin_mm, mask_margin_mm)

        assert error is not None
        assert expected in error


class TestCropPolygon:
    """Polygon の AABB crop と専用 mask 生成."""

    def test_crops_rgb_by_polygon_bounds_plus_margin(self):
        source = _source_image()
        polygon = box(-1.0, -2.0, 1.0, 2.0)
        matrix = np.array([[10.0, 0.0], [0.0, 10.0]])
        shift = np.array([50.0, 40.0])

        crop, error = crop_polygon(
            Image(source),
            polygon,
            matrix,
            shift,
            margin_mm=1.0,
            mask_margin_mm=0.1,
        )

        assert error is None
        assert crop is not None
        assert crop.pixel_rect == (30, 10, 70, 70)
        assert crop.image.shape == (60, 40, 3)
        assert crop.image.dtype == np.uint8
        assert np.array_equal(crop.image, source[10:70, 30:70])
        assert crop.mask.shape == crop.image.shape[:2]
        assert crop.mask.dtype == np.uint8
        assert set(np.unique(crop.mask)) <= {0, 255}

    def test_mask_extends_outside_polygon_by_configured_margin(self):
        crop, _ = crop_polygon(
            _source_image(),
            box(-1.0, -1.0, 1.0, 1.0),
            np.array([[10.0, 0.0], [0.0, 10.0]]),
            np.array([50.0, 50.0]),
            margin_mm=2.0,
            mask_margin_mm=0.1,
        )

        assert crop is not None
        assert crop.mask[30, 19] == 255
        assert crop.mask[30, 18] == 0

    def test_mask_preserves_polygon_holes(self):
        polygon = Polygon(
            [(-2.0, -2.0), (2.0, -2.0), (2.0, 2.0), (-2.0, 2.0)],
            holes=[[(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)]],
        )

        crop, _ = crop_polygon(
            _source_image(),
            polygon,
            np.array([[10.0, 0.0], [0.0, 10.0]]),
            np.array([50.0, 40.0]),
            margin_mm=0.0,
            mask_margin_mm=0.0,
        )

        assert crop is not None
        assert crop.mask[20, 20] == 0
        assert crop.mask[5, 5] == 255

    @pytest.mark.parametrize(
        ("margin_mm", "mask_margin_mm"),
        [(-0.01, 0.0), (1.0, -0.01), (0.1, 0.2)],
    )
    def test_rejects_invalid_margins(self, margin_mm: float, mask_margin_mm: float):
        crop, error = crop_polygon(
            _source_image(),
            box(-1.0, -1.0, 1.0, 1.0),
            np.eye(2),
            np.array([50.0, 40.0]),
            margin_mm=margin_mm,
            mask_margin_mm=mask_margin_mm,
        )

        assert crop is None
        assert error is not None
        assert "margin" in error

    def test_rejects_crop_outside_camera_frame(self):
        crop, error = crop_polygon(
            _source_image(),
            box(-2.0, -2.0, 2.0, 2.0),
            np.array([[10.0, 0.0], [0.0, 10.0]]),
            np.array([5.0, 5.0]),
            margin_mm=0.0,
            mask_margin_mm=0.0,
        )

        assert crop is None
        assert error is not None
        assert "収まりません" in error

    def test_rejects_empty_polygon(self):
        crop, error = crop_polygon(
            _source_image(),
            Polygon(),
            np.eye(2),
            np.array([50.0, 40.0]),
            margin_mm=1.0,
            mask_margin_mm=0.0,
        )

        assert crop is None
        assert error is not None
        assert "polygon" in error
