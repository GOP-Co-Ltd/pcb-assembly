import cv2
import numpy as np
import pytest

from pcbasm.vision import CopperEdgeDetection, CopperEdgeDetector, Image
from tests.helpers import TESTING_DATA_DIR

REFLECTIVE_BOARD_IMAGES = tuple(
    TESTING_DATA_DIR / "vision" / "copper" / f"reflective_board_{index}.png"
    for index in range(3)
)


def _dark_background(size: int = 200) -> np.ndarray:
    """銅箔のない暗い基板風の背景画像を作る."""
    return np.full((size, size, 3), 30, dtype=np.uint8)


def _neighbor_contrast(image: Image) -> float:
    """水平・垂直の隣接画素差から局所コントラストを算出する."""
    gray = image.numpy()[..., 0].astype(float)
    horizontal = np.abs(np.diff(gray, axis=1)).mean()
    vertical = np.abs(np.diff(gray, axis=0)).mean()
    return float(horizontal + vertical)


class TestCopperEdgeDetector:
    """CopperEdgeDetectorクラスのテスト."""

    @pytest.fixture
    def detector(self) -> CopperEdgeDetector:
        """デフォルトパラメータの検出器."""
        return CopperEdgeDetector()

    def test_detect_returns_processed_grayscale_and_binary_edges(
        self, detector: CopperEdgeDetector
    ):
        arr = _dark_background()
        cv2.rectangle(arr, (50, 60), (150, 120), (60, 140, 180), -1)

        detection = detector.detect(Image(arr))

        assert isinstance(detection, CopperEdgeDetection)
        processed = detection.processed.numpy()
        assert processed.shape == arr.shape
        assert processed.dtype == np.uint8
        assert np.array_equal(processed[..., 0], processed[..., 1])
        assert np.array_equal(processed[..., 1], processed[..., 2])
        assert detection.edges.shape == arr.shape[:2]
        assert detection.edges.dtype == np.uint8
        assert set(np.unique(detection.edges)) <= {0, 255}

    def test_detect_edges_returns_binary_mask(self, detector: CopperEdgeDetector):
        arr = _dark_background()
        cv2.rectangle(arr, (50, 60), (150, 120), (60, 140, 180), -1)

        mask = detector.detect_edges(Image(arr))

        assert mask.dtype == np.uint8
        assert mask.shape == (200, 200)
        assert set(np.unique(mask)) <= {0, 255}

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

    def test_detect_edges_remains_compatible_with_detect_result(
        self, detector: CopperEdgeDetector
    ):
        arr = _dark_background()
        cv2.rectangle(arr, (50, 60), (150, 120), (60, 140, 180), -1)
        image = Image(arr)

        assert np.array_equal(
            detector.detect_edges(image), detector.detect(image).edges
        )

    def test_default_sharpen_amount_matches_explicit_half(self):
        arr = _dark_background()
        cv2.rectangle(arr, (50, 60), (150, 120), (60, 140, 180), -1)
        image = Image(arr)

        default = CopperEdgeDetector().detect(image)
        explicit = CopperEdgeDetector(sharpen_amount=0.5).detect(image)

        assert np.array_equal(default.processed.numpy(), explicit.processed.numpy())
        assert np.array_equal(default.edges, explicit.edges)

    @pytest.mark.parametrize(
        ("kwargs", "field"),
        [
            ({"blur_ksize": 0}, "blur_ksize"),
            ({"blur_ksize": -1}, "blur_ksize"),
            ({"blur_ksize": 2}, "blur_ksize"),
            ({"blur_ksize": True}, "blur_ksize"),
            ({"blur_ksize": 3.5}, "blur_ksize"),
            ({"sharpen_amount": -0.1}, "sharpen_amount"),
            ({"sharpen_amount": float("nan")}, "sharpen_amount"),
            ({"sharpen_amount": float("inf")}, "sharpen_amount"),
            ({"sharpen_amount": float("-inf")}, "sharpen_amount"),
            ({"sharpen_amount": True}, "sharpen_amount"),
        ],
    )
    def test_rejects_invalid_preprocessing_parameters(self, kwargs, field):
        with pytest.raises(ValueError, match=field):
            CopperEdgeDetector(**kwargs)

    @pytest.mark.parametrize(
        ("blur_ksize", "sharpen_amount"),
        [(1, 0.0), (3, 0.5), (5, 2.0)],
    )
    def test_accepts_positive_odd_blur_and_nonnegative_sharpen(
        self, blur_ksize: int, sharpen_amount: float
    ):
        detector = CopperEdgeDetector(
            blur_ksize=blur_ksize, sharpen_amount=sharpen_amount
        )

        detection = detector.detect(Image(_dark_background()))

        assert detection.processed.size == (200, 200)


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

    @pytest.mark.parametrize(
        "image_path",
        REFLECTIVE_BOARD_IMAGES,
        ids=lambda path: path.stem,
    )
    def test_sharpen_amount_controls_local_contrast(self, image_path):
        image = Image.load(image_path)

        denoised = CopperEdgeDetector(sharpen_amount=0.0).detect(image)
        enhanced = CopperEdgeDetector(sharpen_amount=2.0).detect(image)

        assert not np.array_equal(
            denoised.processed.numpy(), enhanced.processed.numpy()
        )
        assert _neighbor_contrast(enhanced.processed) > _neighbor_contrast(
            denoised.processed
        )
