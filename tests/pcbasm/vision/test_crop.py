"""固定ピクセル寸法の矩形 crop（vision.crop）の公開契約.

学習データは「全 crop が同一ピクセル寸法」であることが前提なので、ピクセル寸法は
:func:`crop_pixel_size` で 1 回だけ決め、:func:`crop_centered` は中心 rounding で
その寸法の窓を置くだけにする（セルごとの floor / ceil をしない）。
"""

import numpy as np
import pytest

from pcbasm.geometry import Point2d
from pcbasm.vision import Image
from pcbasm.vision.crop import crop_centered, crop_pixel_size

PPM = 10.0
MATRIX = np.array([[PPM, 0.0], [0.0, PPM]])
SHIFT = np.array([50.0, 40.0])
PIXEL_SIZE = 21


def _source_image(width: int = 120, height: int = 100) -> np.ndarray:
    """切り抜き位置と RGB 保持を同時に確認できる合成画像."""
    yy, xx = np.indices((height, width), dtype=np.uint8)
    return np.dstack((xx, yy, xx ^ yy))


def _projected(center: Point2d) -> tuple[float, float]:
    """テスト側で独立に計算した board→pixel 射影（affine と同じ式）."""
    return (center.x * PPM + SHIFT[0], center.y * PPM + SHIFT[1])


def _cropped(center: Point2d, *, pixel_size: int = PIXEL_SIZE):
    crop, error = crop_centered(
        _source_image(), center, MATRIX, SHIFT, pixel_size=pixel_size
    )

    assert error is None
    assert crop is not None
    return crop


class TestCropPixelSize:
    """セル寸法と物理スケールから 1 回だけ整数ピクセル寸法を決める."""

    def test_exact_odd_product_becomes_that_integer(self):
        size, error = crop_pixel_size(2.0, 120.5)

        assert error is None
        assert size == 241

    @pytest.mark.parametrize(
        ("size_mm", "pixel_per_mm"),
        [
            (2.0, 120.5),
            (2.0, 120.0),
            (2.0, 120.3),
            (1.0, 33.4),
            (3.0, 80.0),
            (2.0, 100.7),
            (1.5, 33.33),
            (0.05, 120.5),
        ],
    )
    def test_size_is_a_nearby_positive_odd_integer(
        self, size_mm: float, pixel_per_mm: float
    ):
        size, error = crop_pixel_size(size_mm, pixel_per_mm)

        assert error is None
        assert size is not None
        # 中心画素が 1 つだけ存在するよう奇数、かつ 1 以上
        assert size % 2 == 1
        assert size >= 1
        # 奇数へ寄せるため、積からのずれは最大 1 pixel 強
        assert abs(size - size_mm * pixel_per_mm) < 2.0

    @pytest.mark.parametrize(
        ("size_mm", "pixel_per_mm", "expected"),
        [
            (0.0, 120.5, "size_mm"),
            (-1.0, 120.5, "size_mm"),
            (float("nan"), 120.5, "size_mm"),
            (2.0, 0.0, "pixel_per_mm"),
            (2.0, -10.0, "pixel_per_mm"),
            (2.0, float("inf"), "pixel_per_mm"),
        ],
    )
    def test_rejects_non_positive_or_non_finite_inputs(
        self, size_mm: float, pixel_per_mm: float, expected: str
    ):
        size, error = crop_pixel_size(size_mm, pixel_per_mm)

        assert size is None
        assert error is not None
        assert expected in error


class TestCropCentered:
    """Board 座標の中心を射影し、固定寸法の窓を切り出す."""

    def test_returns_exactly_the_requested_pixel_size(self):
        crop = _cropped(Point2d(2.0, 3.0))

        x0, y0, x1, y1 = crop.pixel_rect
        assert (x1 - x0, y1 - y0) == (PIXEL_SIZE, PIXEL_SIZE)
        assert crop.image.shape == (PIXEL_SIZE, PIXEL_SIZE, 3)
        assert crop.image.dtype == np.uint8

    def test_crop_image_is_the_frame_slice_at_the_crop_rect(self):
        source = _source_image()

        crop, error = crop_centered(
            Image(source), Point2d(2.0, 3.0), MATRIX, SHIFT, pixel_size=PIXEL_SIZE
        )

        assert error is None
        assert crop is not None
        x0, y0, x1, y1 = crop.pixel_rect
        assert np.array_equal(crop.image, source[y0:y1, x0:x1])

    def test_window_is_centered_on_the_projected_point(self):
        center = Point2d(2.0, 3.0)

        crop = _cropped(center)

        pixel_x, pixel_y = _projected(center)
        x0, y0, x1, y1 = crop.pixel_rect
        assert (x0 + x1) / 2 == pytest.approx(pixel_x, abs=1.0)
        assert (y0 + y1) / 2 == pytest.approx(pixel_y, abs=1.0)

    @pytest.mark.parametrize(
        "center",
        [
            Point2d(2.0, 3.0),
            Point2d(2.05, 3.03),
            Point2d(2.049, 2.951),
            Point2d(1.9501, 3.1499),
            Point2d(2.5, 3.5),
        ],
    )
    def test_fractional_pixel_centers_keep_the_same_window_size(self, center: Point2d):
        crop = _cropped(center)

        x0, y0, x1, y1 = crop.pixel_rect
        assert (x1 - x0, y1 - y0) == (PIXEL_SIZE, PIXEL_SIZE)
        pixel_x, pixel_y = _projected(center)
        assert x0 <= pixel_x <= x1
        assert y0 <= pixel_y <= y1

    def test_even_pixel_size_is_honoured_exactly(self):
        crop = _cropped(Point2d(2.0, 3.0), pixel_size=20)

        x0, y0, x1, y1 = crop.pixel_rect
        assert (x1 - x0, y1 - y0) == (20, 20)

    @pytest.mark.parametrize(
        "center", [Point2d(-4.5, 3.0), Point2d(2.0, -3.5), Point2d(20.0, 3.0)]
    )
    def test_window_outside_the_frame_is_reported_without_padding(
        self, center: Point2d
    ):
        crop, error = crop_centered(
            _source_image(), center, MATRIX, SHIFT, pixel_size=PIXEL_SIZE
        )

        assert crop is None
        assert error is not None
        assert "収まりません" in error

    @pytest.mark.parametrize("pixel_size", [0, -1])
    def test_rejects_non_positive_pixel_size(self, pixel_size: int):
        crop, error = crop_centered(
            _source_image(), Point2d(2.0, 3.0), MATRIX, SHIFT, pixel_size=pixel_size
        )

        assert crop is None
        assert error is not None
        assert "pixel_size" in error

    def test_rejects_non_rgb_frame(self):
        gray = np.zeros((100, 120), dtype=np.uint8)

        crop, error = crop_centered(
            gray, Point2d(2.0, 3.0), MATRIX, SHIFT, pixel_size=PIXEL_SIZE
        )

        assert crop is None
        assert error is not None
        assert "3 channel" in error

    @pytest.mark.parametrize(
        ("matrix", "shift"),
        [
            (np.eye(3), np.array([50.0, 40.0])),
            (MATRIX, np.array([50.0, 40.0, 0.0])),
            (np.array([[float("nan"), 0.0], [0.0, PPM]]), np.array([50.0, 40.0])),
        ],
    )
    def test_rejects_malformed_board_to_pixel_affine(
        self, matrix: np.ndarray, shift: np.ndarray
    ):
        crop, error = crop_centered(
            _source_image(), Point2d(2.0, 3.0), matrix, shift, pixel_size=PIXEL_SIZE
        )

        assert crop is None
        assert error is not None
        assert "affine" in error
