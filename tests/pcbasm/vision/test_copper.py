import cv2
import numpy as np
import pytest

from pcbasm.vision import CopperDetector, DetectedCopper, Image


def _dark_background(size: int = 200) -> np.ndarray:
    """銅箔のない暗い基板風の背景画像を作る."""
    return np.full((size, size, 3), 30, dtype=np.uint8)


class TestCopperDetector:
    """CopperDetectorクラスのテスト."""

    @pytest.fixture
    def detector(self) -> CopperDetector:
        """デフォルトパラメータの検出器."""
        return CopperDetector()

    @pytest.fixture
    def image_with_single_region(self) -> Image:
        """暗背景に明色矩形 (50,60)-(150,120) が1つある200x200画像."""
        arr = _dark_background()
        cv2.rectangle(arr, (50, 60), (150, 120), (60, 140, 180), -1)
        return Image(arr)

    @pytest.fixture
    def image_with_two_regions(self) -> Image:
        """大小2つの明色矩形がある200x200画像."""
        arr = _dark_background()
        cv2.rectangle(arr, (20, 20), (120, 120), (60, 140, 180), -1)  # 大
        cv2.rectangle(arr, (140, 140), (180, 180), (60, 140, 180), -1)  # 小
        return Image(arr)

    def test_detect_finds_single_region(
        self, detector: CopperDetector, image_with_single_region: Image
    ):
        regions = detector.detect(image_with_single_region)

        assert len(regions) == 1
        region = regions[0]
        assert isinstance(region, DetectedCopper)
        x, y, w, h = region.bbox
        assert x == pytest.approx(50, abs=5)
        assert y == pytest.approx(60, abs=5)
        assert w == pytest.approx(100, abs=10)
        assert h == pytest.approx(60, abs=10)
        assert region.area_px == pytest.approx(100 * 60, rel=0.2)
        assert region.center.x == pytest.approx(100.0, abs=5.0)
        assert region.center.y == pytest.approx(90.0, abs=5.0)

    def test_detect_returns_empty_for_uniform_image(self, detector: CopperDetector):
        blank_image = Image(_dark_background())

        regions = detector.detect(blank_image)

        assert regions == []

    def test_detect_sorts_by_area_descending(
        self, detector: CopperDetector, image_with_two_regions: Image
    ):
        regions = detector.detect(image_with_two_regions)

        assert len(regions) == 2
        assert regions[0].area_px > regions[1].area_px
        # 大矩形 (20,20)-(120,120) の中心
        assert regions[0].center.x == pytest.approx(70.0, abs=5.0)
        assert regions[0].center.y == pytest.approx(70.0, abs=5.0)
        # 小矩形 (140,140)-(180,180) の中心
        assert regions[1].center.x == pytest.approx(160.0, abs=5.0)
        assert regions[1].center.y == pytest.approx(160.0, abs=5.0)

    def test_min_area_filters_small_regions(self, image_with_two_regions: Image):
        detector = CopperDetector(min_area_px=5000.0)

        regions = detector.detect(image_with_two_regions)

        assert len(regions) == 1
        assert regions[0].area_px >= 5000.0

    def test_compute_mask_returns_binary_mask(
        self, detector: CopperDetector, image_with_single_region: Image
    ):
        mask = detector.compute_mask(image_with_single_region)

        assert mask.dtype == np.uint8
        assert mask.shape == (200, 200)
        assert set(np.unique(mask)) <= {0, 255}
        # 矩形エッジ近傍に非ゼロ画素がある
        assert np.any(mask[55:65, 45:55] > 0)

    def test_compute_mask_blank_for_uniform_image(self, detector: CopperDetector):
        blank_image = Image(_dark_background())

        mask = detector.compute_mask(blank_image)

        assert not np.any(mask)
