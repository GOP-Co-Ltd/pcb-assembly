import cv2
import numpy as np
import pytest

from pcbasm.vision import CopperEdgeDetector, Image
from tests.helpers import TESTING_DATA_DIR

REFLECTIVE_BOARD_IMAGES = tuple(
    TESTING_DATA_DIR / "vision" / "copper" / f"reflective_board_{index}.png"
    for index in range(3)
)


def _dark_background(size: int = 200) -> np.ndarray:
    """銅箔のない暗い基板風の背景画像を作る."""
    return np.full((size, size, 3), 30, dtype=np.uint8)


class TestCopperEdgeDetector:
    """CopperEdgeDetectorクラスのテスト."""

    @pytest.fixture
    def detector(self) -> CopperEdgeDetector:
        """デフォルトパラメータの検出器."""
        return CopperEdgeDetector()

    def test_detect_edges_along_region_boundary(self, detector: CopperEdgeDetector):
        """明色矩形の境界近傍にエッジが出る."""
        arr = _dark_background()
        cv2.rectangle(arr, (50, 60), (150, 120), (60, 140, 180), -1)

        mask = detector.detect_edges(Image(arr))

        # 4辺それぞれの近傍に非ゼロ画素がある
        assert np.any(mask[55:65, 45:55] > 0)  # 左上角付近
        assert np.any(mask[55:65, 145:155] > 0)  # 右上角付近
        assert np.any(mask[115:125, 45:55] > 0)  # 左下角付近
        # 矩形内部（境界から離れた一様部分）にはエッジが出ない
        assert not np.any(mask[80:100, 80:120] > 0)

    def test_detect_edges_for_unclosed_boundary(self, detector: CopperEdgeDetector):
        """銅箔が撮像範囲からはみ出し境界が閉じない場合でもエッジが出る."""
        arr = _dark_background()
        # 右辺が画像外にはみ出す矩形 → フレーム内では境界が開いている
        cv2.rectangle(arr, (100, 50), (250, 150), (60, 140, 180), -1)

        mask = detector.detect_edges(Image(arr))

        # 見えている左辺の近傍にエッジが出る
        assert np.any(mask[60:140, 95:105] > 0)

    def test_detect_edges_blank_for_uniform_image(self, detector: CopperEdgeDetector):
        mask = detector.detect_edges(Image(_dark_background()))

        assert not np.any(mask)

    def test_high_thresholds_suppress_weak_edges(self):
        """コントラストの弱い境界は閾値を上げると検出されなくなる."""
        arr = _dark_background()
        cv2.rectangle(arr, (50, 60), (150, 120), (45, 45, 45), -1)  # 低コントラスト

        sensitive = CopperEdgeDetector(canny_low=5.0, canny_high=15.0)
        strict = CopperEdgeDetector(canny_low=100.0, canny_high=200.0)

        assert np.any(sensitive.detect_edges(Image(arr)))
        assert not np.any(strict.detect_edges(Image(arr)))

    def test_blur_kernel_one_preserves_grayscale_pixels(self):
        arr = _dark_background()
        cv2.rectangle(arr, (50, 60), (150, 120), (60, 140, 180), -1)

        processed = CopperEdgeDetector(blur_ksize=1).detect(Image(arr)).processed

        expected = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
        assert np.array_equal(processed.numpy()[..., 0], expected)

    @pytest.mark.parametrize(
        ("kwargs", "field"),
        [
            ({"blur_ksize": 0}, "blur_ksize"),
            ({"blur_ksize": -1}, "blur_ksize"),
            ({"blur_ksize": 2}, "blur_ksize"),
            ({"blur_ksize": True}, "blur_ksize"),
            ({"blur_ksize": 3.5}, "blur_ksize"),
        ],
    )
    def test_rejects_invalid_preprocessing_parameters(self, kwargs, field):
        with pytest.raises(ValueError, match=field):
            CopperEdgeDetector(**kwargs)


class TestCopperEdgeDetectorReflectiveBoards:
    """反射・ハイライトを含む実撮像画像で前処理の公開結果を検証する."""

    @pytest.mark.parametrize(
        "image_path",
        REFLECTIVE_BOARD_IMAGES,
        ids=lambda path: path.stem,
    )
    def test_detect_processes_real_image_with_stable_output_contract(self, image_path):
        image = Image.load(image_path)

        detection = CopperEdgeDetector().detect(image)

        assert detection.processed.size == image.size
        assert detection.processed.numpy().dtype == np.uint8
        assert detection.edges.shape == (image.height, image.width)
        assert detection.edges.dtype == np.uint8
        assert set(np.unique(detection.edges)) <= {0, 255}
        assert np.any(detection.edges)
