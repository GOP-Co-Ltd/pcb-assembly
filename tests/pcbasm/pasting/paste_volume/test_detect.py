"""塗布前後画像から円の直径を測る検出器の公開契約.

実素材は ``data/testing/paste-volume/``（出典は同 directory の README）。合成画像では
銅板テクスチャ・照明ムラ・ペーストの質感が再現できないので、実データで固める。

OpenCV のバージョンで下位桁が動きうるため、具体的な小数値はピンしない。固定するのは
blank の厳密な 0、量の順序関係、view 間の一致、ハイパラの効き方。
"""

import cv2
import numpy as np
import pytest

from pcbasm.pasting.paste_volume.detect import (
    DotDetectionSpec,
    DotMeasurement,
    detection_mask,
    measure_dot,
)
from pcbasm.vision.image import ImageArray
from tests.helpers import TESTING_DATA_DIR

MATERIAL_DIR = TESTING_DATA_DIR / "paste-volume"
PIXEL_PER_MM = 28.677782176153425

# 素材の指令量が小さい順。直径もこの順で並ぶ
VOLUME_ORDER = ("small", "medium", "large")


def _load(name: str, phase: str, view: int) -> ImageArray:
    path = MATERIAL_DIR / name / phase / f"{view:02d}.png"
    image = cv2.imread(str(path))
    assert image is not None, path
    return image


def _measure(name: str, view: int = 0, **overrides: float | int) -> DotMeasurement:
    spec = DotDetectionSpec(**overrides)  # type: ignore[arg-type]
    measurement, error = measure_dot(
        _load(name, "pre", view),
        _load(name, "post", view),
        pixel_per_mm=PIXEL_PER_MM,
        spec=spec,
    )

    assert error is None, error
    assert measurement is not None
    return measurement


class TestMeasureDotOnRealMaterial:
    """実素材での検出。blank=0 と量の順序が要の契約."""

    def test_blank_cell_measures_exactly_zero(self):
        for view in (0, 1):
            measurement = _measure("blank", view)

            assert measurement.diameter_mm == 0.0
            assert measurement.detected is False
            assert measurement.area_px == 0

    def test_every_dispensed_cell_is_detected(self):
        for name in VOLUME_ORDER:
            for view in (0, 1):
                measurement = _measure(name, view)

                assert measurement.detected is True
                assert 0.2 < measurement.diameter_mm < 1.2, (name, view)

    def test_diameter_grows_with_the_commanded_volume(self):
        diameters = [_measure(name).diameter_mm for name in VOLUME_ORDER]

        assert diameters == sorted(diameters)
        assert diameters[0] < diameters[-1]

    def test_views_of_the_same_cell_agree(self):
        for name in VOLUME_ORDER:
            first = _measure(name, 0).diameter_mm
            second = _measure(name, 1).diameter_mm

            assert abs(first - second) / max(first, second) < 0.15, name

    def test_reports_the_contrast_it_measured(self):
        blank = _measure("blank")
        large = _measure("large")

        # blank ガードは差分の分位点で効く。塗布点のほうが必ず大きい
        assert blank.contrast < large.contrast


class TestMeasureDotHyperParameters:
    """ハイパラが実際に効くこと（既定値を変えて挙動が動く）."""

    def test_raising_the_minimum_contrast_suppresses_a_real_deposit(self):
        detected = _measure("large")
        suppressed = _measure("large", min_contrast=250.0)

        assert detected.detected is True
        assert suppressed.detected is False
        assert suppressed.diameter_mm == 0.0

    def test_minimum_area_discards_a_small_component(self):
        kept = _measure("small")
        discarded = _measure("small", min_area_px=100_000)

        assert kept.detected is True
        assert discarded.detected is False

    def test_disabling_the_open_kernel_keeps_the_deposit_detected(self):
        assert _measure("large", open_kernel_px=0).detected is True


class TestMeasureDotRejections:
    """構造的に不正な入力だけを ``(None, 理由)`` で返す."""

    def test_rejects_mismatched_shapes(self):
        measurement, error = measure_dot(
            _load("large", "pre", 0),
            _load("large", "post", 0)[:10, :10],
            pixel_per_mm=PIXEL_PER_MM,
        )

        assert measurement is None
        assert error is not None

    @pytest.mark.parametrize("pixel_per_mm", [0.0, -1.0, float("nan")])
    def test_rejects_non_positive_scale(self, pixel_per_mm: float):
        measurement, error = measure_dot(
            _load("large", "pre", 0),
            _load("large", "post", 0),
            pixel_per_mm=pixel_per_mm,
        )

        assert measurement is None
        assert error is not None
        assert repr(pixel_per_mm) in error

    def test_rejects_a_single_channel_image(self):
        gray = cv2.cvtColor(_load("large", "pre", 0), cv2.COLOR_BGR2GRAY)

        measurement, error = measure_dot(gray, gray, pixel_per_mm=PIXEL_PER_MM)

        assert measurement is None
        assert error is not None

    def test_rejects_an_invalid_spec(self):
        measurement, error = measure_dot(
            _load("large", "pre", 0),
            _load("large", "post", 0),
            pixel_per_mm=PIXEL_PER_MM,
            spec=DotDetectionSpec(min_area_px=-1),
        )

        assert measurement is None
        assert error is not None

    def test_an_unchanged_pair_is_not_a_failure(self):
        """はんだが写っていないのは正常系（blank と同じ扱い）."""
        pre = _load("blank", "pre", 0)

        measurement, error = measure_dot(pre, pre, pixel_per_mm=PIXEL_PER_MM)

        assert error is None
        assert measurement is not None
        assert measurement.diameter_mm == 0.0


class TestMeasureDotIsScaleAware:
    """直径は mm。``pixel_per_mm`` に反比例する."""

    def test_halving_the_scale_doubles_the_diameter(self):
        base = _measure("large").diameter_mm
        halved, error = measure_dot(
            _load("large", "pre", 0),
            _load("large", "post", 0),
            pixel_per_mm=PIXEL_PER_MM / 2.0,
        )

        assert error is None
        assert halved is not None
        assert halved.diameter_mm == pytest.approx(base * 2.0)


class TestDetectionMask:
    """面積を数えた 2 値マスクの目視用取り出し."""

    def test_returns_a_mask_shaped_like_the_input(self):
        mask, error = detection_mask(
            _load("large", "pre", 0), _load("large", "post", 0)
        )

        assert error is None
        assert mask is not None
        assert mask.shape == _load("large", "pre", 0).shape[:2]

    def test_marks_as_many_pixels_as_the_measured_area(self):
        pre, post = _load("large", "pre", 0), _load("large", "post", 0)
        mask, _ = detection_mask(pre, post)
        assert mask is not None

        # 最大連結成分だけを数える measure_dot 以上にはならず、下回りもしない
        assert int(np.count_nonzero(mask)) >= _measure("large").area_px

    def test_marks_more_pixels_for_a_larger_deposit(self):
        counts = []
        for name in VOLUME_ORDER:
            mask, _ = detection_mask(_load(name, "pre", 0), _load(name, "post", 0))
            assert mask is not None
            counts.append(int(np.count_nonzero(mask)))

        assert counts == sorted(counts)

    def test_returns_an_empty_mask_for_a_blank_cell(self):
        mask, error = detection_mask(
            _load("blank", "pre", 0), _load("blank", "post", 0)
        )

        assert error is None
        assert mask is not None
        assert int(np.count_nonzero(mask)) == 0

    def test_reports_a_shape_mismatch(self):
        mask, error = detection_mask(
            _load("large", "pre", 0), np.zeros((10, 10, 3), dtype=np.uint8)
        )

        assert mask is None
        assert error is not None

    def test_reports_an_invalid_spec(self):
        mask, error = detection_mask(
            _load("large", "pre", 0),
            _load("large", "post", 0),
            spec=DotDetectionSpec(open_kernel_px=2),
        )

        assert mask is None
        assert error is not None
