"""基準点マーカーの検出器の仕様テスト.

基板の位置計測、基準点の教示、プレビューが同じ検出器で同じ円を見る。
"""

import cv2
import numpy as np
import pytest

from pcbasm.vision import Image, reference_marker_detector
from tests.support.camera_model import make_camera_model

PIXEL_PER_MM = 40.0
SIZE = (600, 600)
TARGET_DIAMETER = 3.0


def _marker_image(diameter_mm: float) -> Image:
    """中心に直径 ``diameter_mm`` の黒い円がある画像."""
    arr = np.full((SIZE[1], SIZE[0], 3), 255, dtype=np.uint8)
    radius_px = round(diameter_mm / 2 * PIXEL_PER_MM)
    cv2.circle(arr, (SIZE[0] // 2, SIZE[1] // 2), radius_px, (0, 0, 0), -1)
    return Image(arr)


def _detects(diameter_mm: float, tolerance_mm: float) -> bool:
    detector = reference_marker_detector(
        make_camera_model(PIXEL_PER_MM, SIZE),
        SIZE,
        target_diameter_mm=TARGET_DIAMETER,
        diameter_tolerance_mm=tolerance_mm,
    )
    return detector.detect_nearest_center(_marker_image(diameter_mm)) is not None


class TestReferenceMarkerDetector:
    @pytest.mark.parametrize("diameter_mm", [3.0, 3.3])
    def test_detects_marker_within_tolerance(self, diameter_mm: float):
        assert _detects(diameter_mm, tolerance_mm=0.5)

    @pytest.mark.parametrize("diameter_mm", [2.2, 3.8])
    def test_ignores_circle_outside_tolerance(self, diameter_mm: float):
        assert not _detects(diameter_mm, tolerance_mm=0.5)

    @pytest.mark.parametrize("diameter_mm", [2.2, 3.8])
    def test_wider_tolerance_accepts_the_same_circle(self, diameter_mm: float):
        # 以前のプレビューの許容誤差 1.0 mm では、ジョブが見ない円も見えていた
        assert _detects(diameter_mm, tolerance_mm=1.0)

    def test_looks_only_inside_the_crop(self):
        detector = reference_marker_detector(
            make_camera_model(PIXEL_PER_MM, (1280, 720)),
            SIZE,
            target_diameter_mm=TARGET_DIAMETER,
            diameter_tolerance_mm=0.5,
        )
        arr = np.full((720, 1280, 3), 255, dtype=np.uint8)
        cv2.circle(arr, (640 + 400, 360), 60, (0, 0, 0), -1)

        assert detector.detect_nearest_center(Image(arr)) is None
