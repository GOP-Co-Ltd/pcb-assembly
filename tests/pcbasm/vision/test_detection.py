import math

import cv2
import numpy as np
import pytest

from pcbasm.vision import Image
from pcbasm.vision.detection import (
    CircleDetector,
    OffsetStatistics,
    PasteDotDetector,
    validate_paste_diameters,
)
from pcbasm.vision.dot import DotDetectionSpec
from tests.helpers import PASTE_VOLUME_MATERIAL_DIR, PASTE_VOLUME_PIXEL_PER_MM


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

    def test_blank_image_yields_no_circle(self, detector: CircleDetector):
        blank_image = Image(np.full((200, 200, 3), 255, dtype=np.uint8))

        assert detector.detect_circles(blank_image) == []
        assert detector.detect_nearest_center(blank_image) is None

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

    def test_detect_with_statistics_returns_none_below_minimum_sample_count(
        self, detector: CircleDetector
    ):
        """検出結果があっても必要数に届かなければNoneを返す."""
        result = detector.detect_with_statistics(
            [self._circle_at(110), self._blank()],
            minimum_sample_count=2,
        )

        assert result is None

    @pytest.mark.parametrize("minimum_sample_count", [0, -1, True, 1.5])
    def test_detect_with_statistics_rejects_invalid_minimum_sample_count(
        self, detector: CircleDetector, minimum_sample_count
    ):
        with pytest.raises(ValueError) as exc_info:
            detector.detect_with_statistics(
                [],
                minimum_sample_count=minimum_sample_count,
            )

        assert "minimum_sample_count" in str(exc_info.value)

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

    @staticmethod
    def _circle_at(center_x: int) -> Image:
        array = np.full((200, 200, 3), 255, dtype=np.uint8)
        cv2.circle(array, (center_x, 100), 15, (0, 0, 0), -1)
        return Image(array)

    @staticmethod
    def _blank() -> Image:
        return Image(np.full((200, 200, 3), 255, dtype=np.uint8))


# 実素材 `data/testing/paste-volume/` の参考直径 [mm]（出典は同 directory の README）。
# OpenCV のバージョンで下位桁が動きうるので広めの許容で突き合わせる
MATERIAL_DIAMETER_MM: tuple[tuple[str, float], ...] = (
    ("small", 0.587),
    ("medium", 0.763),
    ("large", 0.865),
)
DIAMETER_TOLERANCE_MM = 0.05

# ツールヘッドオフセットジョブが渡す直径帯の既定値（web.api.jobs.pasting.toolhead_offset）
JOB_DEFAULT_DIAMETER_MIN_MM = 0.4
JOB_DEFAULT_DIAMETER_MAX_MM = 2.0


def _material(name: str, view: int = 0) -> Image:
    """実素材の塗布後画像（塗布前画像は使わない）."""
    path = PASTE_VOLUME_MATERIAL_DIR / name / "post" / f"{view:02d}.png"
    array = cv2.imread(str(path))
    assert array is not None, path
    return Image(array)


def _shifted(image: Image, dx: int, dy: int) -> Image:
    """既知量だけ平行移動した画像（縁は反射で埋める）."""
    matrix = np.array([[1.0, 0.0, dx], [0.0, 1.0, dy]], dtype=np.float32)
    return Image(
        cv2.warpAffine(
            image.numpy(),
            matrix,
            (image.width, image.height),
            borderMode=cv2.BORDER_REFLECT,
        )
    )


class TestPasteDotDetector:
    """塗布痕（暗い円）を単一フレームの背景差分 + Otsu で検出する契約.

    実素材は ``data/testing/paste-volume/`` の塗布後画像。合成画像では銅板テクスチャ・
    照明ムラ・ペーストの質感が再現できないので、検出できる/できないの要件は実素材で固める。
    """

    @pytest.fixture
    def detector(self) -> PasteDotDetector:
        """実素材の収集条件に合わせた検出器.

        直径帯はツールヘッドオフセットジョブの出荷既定（`paste_diameter_min` /
        `paste_diameter_max`）と同じ値にする。未塗布を弾く要件は出荷構成で
        成立しなければ意味がない。
        """
        return PasteDotDetector(
            pixel_per_mm=PASTE_VOLUME_PIXEL_PER_MM,
            diameter_min_mm=JOB_DEFAULT_DIAMETER_MIN_MM,
            diameter_max_mm=JOB_DEFAULT_DIAMETER_MAX_MM,
        )

    @pytest.mark.parametrize(("name", "diameter_mm"), MATERIAL_DIAMETER_MM)
    @pytest.mark.parametrize("view", [0, 1])
    def test_detects_real_paste_dot(
        self,
        detector: PasteDotDetector,
        name: str,
        diameter_mm: float,
        view: int,
    ):
        detected = detector.detect_nearest_center(_material(name, view))

        assert detected is not None
        measured_mm = 2.0 * detected.radius / PASTE_VOLUME_PIXEL_PER_MM
        assert measured_mm == pytest.approx(diameter_mm, abs=DIAMETER_TOLERANCE_MM)
        # 素材は塗布痕を中心に切り出してあるので、ズレは ROI 中心の近傍に収まる
        assert detected.offset.mm.norm < 0.2

    @pytest.mark.parametrize("view", [0, 1])
    def test_blank_material_is_not_detected(
        self, detector: PasteDotDetector, view: int
    ):
        """塗布していないセルは検出なし（背景ノイズを円と取り違えない）.

        未塗布板の 2 値化には 1.2 mm 相当の大きな成分（円形度 0.2 以下）と
        0.1-0.3 mm の小片（円形度は 1 を超える）が残る。前者を円形度、後者を
        直径下限で落として初めて「検出なし」になる。
        """
        assert detector.detect_nearest_center(_material("blank", view)) is None

    def test_blank_material_needs_the_diameter_floor(self):
        """直径下限を外すと未塗布板の小片を拾う（下限が効いている証拠）."""
        without_floor = PasteDotDetector(
            pixel_per_mm=PASTE_VOLUME_PIXEL_PER_MM,
            diameter_min_mm=0.01,
            diameter_max_mm=JOB_DEFAULT_DIAMETER_MAX_MM,
        )

        assert without_floor.detect_nearest_center(_material("blank", 0)) is not None

    def test_offset_follows_known_translation(self, detector: PasteDotDetector):
        original = detector.detect_nearest_center(_material("medium"))
        shifted = detector.detect_nearest_center(_shifted(_material("medium"), 6, -4))

        assert original is not None and shifted is not None
        assert shifted.offset.px.x == pytest.approx(original.offset.px.x + 6, abs=0.5)
        assert shifted.offset.px.y == pytest.approx(original.offset.px.y - 4, abs=0.5)

    @pytest.mark.parametrize(
        ("diameter_min_mm", "diameter_max_mm"),
        [(1.0, 2.0), (0.1, 0.5)],
    )
    def test_rejects_dot_outside_diameter_range(
        self, diameter_min_mm: float, diameter_max_mm: float
    ):
        detector = PasteDotDetector(
            pixel_per_mm=PASTE_VOLUME_PIXEL_PER_MM,
            diameter_min_mm=diameter_min_mm,
            diameter_max_mm=diameter_max_mm,
        )

        assert detector.detect_nearest_center(_material("medium")) is None

    def test_rejects_spot_that_is_not_round(self, detector: PasteDotDetector):
        """同面積でも細長い成分（ヘアライン等）は円形度で落とす."""
        bar = np.full((120, 120, 3), 200, dtype=np.uint8)
        cv2.rectangle(bar, (40, 56), (79, 63), (80, 80, 80), -1)
        disk = np.full((120, 120, 3), 200, dtype=np.uint8)
        cv2.circle(disk, (60, 60), 10, (80, 80, 80), -1)

        assert detector.detect_nearest_center(Image(bar)) is None
        assert detector.detect_nearest_center(Image(disk)) is not None

    @pytest.mark.parametrize("highlight_radius", [4, 8])
    def test_detects_a_glossy_dot_whose_center_is_a_highlight(
        self, detector: PasteDotDetector, highlight_radius: int
    ):
        """光沢で中心が抜けた塗布痕も、外径と円形度が中実の円と同じになる."""
        array = np.full((120, 120, 3), 200, dtype=np.uint8)
        cv2.circle(array, (60, 60), 12, (80, 80, 80), -1)
        cv2.circle(array, (60, 60), highlight_radius, (205, 205, 205), -1)

        detected = detector.detect_nearest_center(Image(array))

        assert detected is not None
        assert detected.center.x == pytest.approx(60.0, abs=1.0)
        assert detected.center.y == pytest.approx(60.0, abs=1.0)
        solid_diameter_mm = 2.0 * 12 / PASTE_VOLUME_PIXEL_PER_MM
        measured_mm = 2.0 * detected.radius / PASTE_VOLUME_PIXEL_PER_MM
        assert measured_mm == pytest.approx(solid_diameter_mm, abs=0.05)

    def test_uniform_image_is_not_detected(self, detector: PasteDotDetector):
        uniform = Image(np.full((120, 120, 3), 150, dtype=np.uint8))

        assert detector.detect_nearest_center(uniform) is None

    def test_selects_the_dot_nearest_to_center(self, detector: PasteDotDetector):
        array = np.full((200, 200, 3), 200, dtype=np.uint8)
        cv2.circle(array, (110, 105), 10, (80, 80, 80), -1)
        cv2.circle(array, (40, 160), 10, (80, 80, 80), -1)

        detected = detector.detect_nearest_center(Image(array))

        assert detected is not None
        assert detected.center.x == pytest.approx(110.0, abs=1.0)
        assert detected.center.y == pytest.approx(105.0, abs=1.0)

    def test_crop_size_limits_detection_area(self):
        detector = PasteDotDetector(
            pixel_per_mm=PASTE_VOLUME_PIXEL_PER_MM,
            diameter_min_mm=JOB_DEFAULT_DIAMETER_MIN_MM,
            diameter_max_mm=JOB_DEFAULT_DIAMETER_MAX_MM,
            crop_size=(100, 100),
        )
        array = np.full((200, 200, 3), 200, dtype=np.uint8)
        cv2.circle(array, (20, 20), 10, (80, 80, 80), -1)

        assert detector.detect_nearest_center(Image(array)) is None

    def test_detect_with_statistics_aggregates_frames(self, detector: PasteDotDetector):
        """基底クラスの統計集約が実素材でも効く（3 フレームの平均と母標準偏差）."""
        images = [_shifted(_material("medium"), dx, 0) for dx in (2, 4, 6)]

        statistics = detector.detect_with_statistics(images, minimum_sample_count=3)

        assert statistics is not None
        assert statistics.sample_count == 3
        base = detector.detect_nearest_center(_material("medium"))
        assert base is not None
        assert statistics.mean.x == pytest.approx(base.offset.px.x + 4, abs=0.5)
        assert statistics.std.x == pytest.approx(math.sqrt(8 / 3), abs=0.3)

    def test_detect_with_statistics_returns_none_when_blank(
        self, detector: PasteDotDetector
    ):
        assert detector.detect_with_statistics([_material("blank")] * 3) is None

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"pixel_per_mm": 0.0},
            {"pixel_per_mm": float("nan")},
            {"diameter_min_mm": 2.0, "diameter_max_mm": 1.0},
            {"diameter_min_mm": 0.0},
            {"diameter_min_mm": -1.0},
            {"diameter_max_mm": float("inf")},
            {"min_circularity": -0.1},
            {"min_circularity": 1.5},
            {"spec": DotDetectionSpec(open_kernel_px=2)},
        ],
    )
    def test_rejects_invalid_arguments(self, kwargs: dict[str, object]):
        arguments: dict[str, object] = {
            "pixel_per_mm": PASTE_VOLUME_PIXEL_PER_MM,
            "diameter_min_mm": 0.3,
            "diameter_max_mm": 2.0,
            **kwargs,
        }

        with pytest.raises(ValueError):
            PasteDotDetector(**arguments)  # type: ignore[arg-type]


class TestValidatePasteDiameters:
    def test_accepts_positive_minimum_below_maximum(self):
        assert validate_paste_diameters(0.1, 2.0) is None
        assert validate_paste_diameters(0.5, 2.0) is None

    @pytest.mark.parametrize(
        ("diameter_min", "diameter_max"),
        [
            (0.0, 2.0),
            (-0.1, 2.0),
            (2.0, 2.0),
            (2.5, 2.0),
            (float("nan"), 2.0),
            (0.5, float("inf")),
        ],
    )
    def test_rejects_unordered_or_non_finite_range(
        self, diameter_min: float, diameter_max: float
    ):
        error = validate_paste_diameters(diameter_min, diameter_max)

        assert error is not None
        assert "最小直径 < 最大直径" in error
