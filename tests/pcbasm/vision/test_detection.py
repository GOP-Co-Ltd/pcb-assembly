import math

import cv2
import numpy as np
import pytest

from pcbasm.vision import Image
from pcbasm.vision.detection import (
    CircleDetector,
    DetectedCircle,
    OffsetStatistics,
)


class TestCircleDetector:
    """CircleDetectorクラスのテスト."""

    @pytest.fixture
    def detector(self) -> CircleDetector:
        """標準的な検出器 (10 pixel/mm, 直径3mm)."""
        return CircleDetector(
            pixel_per_mm=10.0,
            target_diameter_mm=3.0,
            diameter_tolerance_mm=1.0,
        )

    @pytest.fixture
    def image_with_center_circle(self) -> Image:
        """中心に直径30pxの円がある200x200画像."""
        arr = np.full((200, 200, 3), 255, dtype=np.uint8)
        cv2.circle(arr, (100, 100), 15, (0, 0, 0), -1)
        return Image(arr)

    @pytest.fixture
    def image_with_offset_circle(self) -> Image:
        """中心から(20, 30)ずれた位置に円がある200x200画像."""
        arr = np.full((200, 200, 3), 255, dtype=np.uint8)
        cv2.circle(arr, (120, 130), 15, (0, 0, 0), -1)
        return Image(arr)

    def test_detect_circles_finds_circle(
        self, detector: CircleDetector, image_with_center_circle: Image
    ):
        circles = detector.detect_circles(image_with_center_circle)

        assert len(circles) == 1
        assert isinstance(circles[0], DetectedCircle)

    def test_detect_circles_returns_empty_when_no_circle(
        self, detector: CircleDetector
    ):
        blank_image = Image(np.full((200, 200, 3), 255, dtype=np.uint8))

        circles = detector.detect_circles(blank_image)

        assert circles == []

    def test_detect_nearest_center_returns_circle_at_center(
        self, detector: CircleDetector, image_with_center_circle: Image
    ):
        result = detector.detect_nearest_center(image_with_center_circle)

        assert result is not None
        assert result.center.x == pytest.approx(100.0, abs=2.0)
        assert result.center.y == pytest.approx(100.0, abs=2.0)
        assert result.offset.px.norm == pytest.approx(0.0, abs=2.0)
        assert result.offset.mm.norm == pytest.approx(0.0, abs=0.2)

    def test_detect_nearest_center_calculates_offset(
        self, detector: CircleDetector, image_with_offset_circle: Image
    ):
        result = detector.detect_nearest_center(image_with_offset_circle)

        assert result is not None
        # 画像中心(100, 100)から円中心(120, 130)へのオフセット
        assert result.offset.px.x == pytest.approx(20.0, abs=2.0)
        assert result.offset.px.y == pytest.approx(30.0, abs=2.0)
        # mm単位 (10 pixel/mm)
        assert result.offset.mm.x == pytest.approx(2.0, abs=0.2)
        assert result.offset.mm.y == pytest.approx(3.0, abs=0.2)

    def test_detect_nearest_center_returns_none_when_no_circle(
        self, detector: CircleDetector
    ):
        blank_image = Image(np.full((200, 200, 3), 255, dtype=np.uint8))

        result = detector.detect_nearest_center(blank_image)

        assert result is None

    def test_detect_nearest_center_filters_by_size(self):
        """ターゲットサイズと異なる円は無視される."""
        # 直径10mm (100px) を期待する検出器
        detector = CircleDetector(
            pixel_per_mm=10.0,
            target_diameter_mm=10.0,
            diameter_tolerance_mm=1.0,
        )
        # 直径30pxの小さな円しかない画像
        arr = np.full((200, 200, 3), 255, dtype=np.uint8)
        cv2.circle(arr, (100, 100), 15, (0, 0, 0), -1)

        result = detector.detect_nearest_center(Image(arr))

        assert result is None

    def test_detect_nearest_center_selects_nearest_to_center(self):
        """複数の円がある場合、最も中心に近いものを選択."""
        detector = CircleDetector(
            pixel_per_mm=10.0,
            target_diameter_mm=3.0,
            diameter_tolerance_mm=1.0,
        )
        arr = np.full((200, 200, 3), 255, dtype=np.uint8)
        # 中心に近い円 (105, 105)
        cv2.circle(arr, (105, 105), 15, (0, 0, 0), -1)
        # 中心から遠い円 (150, 150)
        cv2.circle(arr, (150, 150), 15, (0, 0, 0), -1)

        result = detector.detect_nearest_center(Image(arr))

        assert result is not None
        assert result.center.x == pytest.approx(105.0, abs=3.0)
        assert result.center.y == pytest.approx(105.0, abs=3.0)

    def test_crop_size_limits_detection_area(self):
        """crop_sizeが指定された場合、関心領域のみを検出対象とする."""
        detector = CircleDetector(
            pixel_per_mm=10.0,
            target_diameter_mm=3.0,
            diameter_tolerance_mm=1.0,
            crop_size=(100, 100),
        )
        # 200x200画像の端に円を配置（crop後は含まれない）
        arr = np.full((200, 200, 3), 255, dtype=np.uint8)
        cv2.circle(arr, (20, 20), 15, (0, 0, 0), -1)

        result = detector.detect_nearest_center(Image(arr))

        assert result is None

    def test_detect_with_statistics_calculates_mean_and_std(
        self, detector: CircleDetector
    ):
        """複数画像から平均と標準偏差を計算."""
        images = []
        # 異なる位置に円がある3つの画像を作成
        for offset_x in [10, 20, 30]:
            arr = np.full((200, 200, 3), 255, dtype=np.uint8)
            cv2.circle(arr, (100 + offset_x, 100), 15, (0, 0, 0), -1)
            images.append(Image(arr))

        result = detector.detect_with_statistics(images)

        assert result is not None
        assert isinstance(result, OffsetStatistics)
        assert result.sample_count == 3
        expected_mean = (10 + 20 + 30) / 3
        expected_std = math.sqrt(  #  sqrt{(10-20)² + (20-20)² + (30-20)² / 3} ≈ 8.16
            (
                (10 - expected_mean) ** 2
                + (20 - expected_mean) ** 2
                + (30 - expected_mean) ** 2
            )
            / 3
        )
        assert result.mean.x == pytest.approx(expected_mean, abs=3.0)
        assert result.mean.y == pytest.approx(0.0, abs=3.0)
        assert result.std.x == pytest.approx(expected_std, abs=2.0)
        # mm単位 (10 pixel/mm)
        assert result.mean_mm.x == pytest.approx(expected_mean / 10.0, abs=0.3)

    def test_detect_with_statistics_returns_none_for_empty_images(
        self, detector: CircleDetector
    ):
        """空のイテラブルの場合はNoneを返す."""
        result = detector.detect_with_statistics([])

        assert result is None

    def test_detect_with_statistics_returns_none_when_all_fail(
        self, detector: CircleDetector
    ):
        """全ての画像で検出失敗時はNoneを返す."""
        blank_images = [
            Image(np.full((200, 200, 3), 255, dtype=np.uint8)) for _ in range(3)
        ]

        result = detector.detect_with_statistics(blank_images)

        assert result is None

    def test_detect_with_statistics_ignores_failed_detections(
        self, detector: CircleDetector
    ):
        """一部の画像で検出失敗しても、成功した画像から統計を計算."""
        images = []
        # 円がある画像
        for offset_x in [10, 20]:
            arr = np.full((200, 200, 3), 255, dtype=np.uint8)
            cv2.circle(arr, (100 + offset_x, 100), 15, (0, 0, 0), -1)
            images.append(Image(arr))
        # 円がない画像
        images.append(Image(np.full((200, 200, 3), 255, dtype=np.uint8)))

        result = detector.detect_with_statistics(images)

        assert result is not None
        assert result.sample_count == 2

    def test_detect_with_statistics_single_image(self, detector: CircleDetector):
        """1画像のみの場合、stdは0."""
        arr = np.full((200, 200, 3), 255, dtype=np.uint8)
        cv2.circle(arr, (110, 100), 15, (0, 0, 0), -1)

        result = detector.detect_with_statistics([Image(arr)])

        assert result is not None
        assert result.sample_count == 1
        assert result.mean.x == pytest.approx(10.0, abs=2.0)
        assert result.std.x == pytest.approx(0.0)
        assert result.std.y == pytest.approx(0.0)
