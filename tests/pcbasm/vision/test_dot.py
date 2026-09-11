"""暗い斑点（塗布痕）の 2 値化と連結成分の公開契約.

塗布前後差分を使う :mod:`pcbasm.pasting.paste_volume.detect` と、単一フレームから
背景を推定する :class:`~pcbasm.vision.detection.PasteDotDetector` が共有する段。
実素材での回帰は各利用側のテストが持ち、ここは段ごとの振る舞いを固める。
"""

import math

import cv2
import numpy as np
import pytest

from pcbasm.vision.dot import (
    DotDetectionSpec,
    background_darkening,
    background_kernel_px,
    dark_spots,
    darkening,
    fill_dark_spot_holes,
    segment_darkening,
)
from pcbasm.vision.image import ImageArray


def _gray(value: int, size: int = 120) -> ImageArray:
    return np.full((size, size, 3), value, dtype=np.uint8)


def _mask_with_disk(center: tuple[int, int], radius: int) -> ImageArray:
    mask = np.zeros((120, 120), dtype=np.uint8)
    cv2.circle(mask, center, radius, 255, -1)
    return mask


class TestDotDetectionSpec:
    """検出ハイパラの None 返却バリデーション."""

    def test_default_spec_is_valid(self):
        assert DotDetectionSpec().validate() is None

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("min_contrast", -1.0),
            ("min_contrast", float("nan")),
            ("contrast_percentile", 0.0),
            ("contrast_percentile", 100.1),
            ("threshold_floor_ratio", -0.1),
            ("threshold_floor_ratio", float("inf")),
            ("open_kernel_px", -1),
            ("open_kernel_px", 2),
            ("min_area_px", -1),
            # 0 を許すと面積 0 の成分が「検出できた」ことになり、「検出できた」と
            # 「直径が正」の対応が壊れる
            ("min_area_px", 0),
        ],
    )
    def test_rejects_invalid_field_and_names_the_offending_value(
        self, field: str, value: float | int
    ):
        error = DotDetectionSpec(**{field: value}).validate()  # type: ignore[arg-type]

        assert error is not None
        assert repr(value) in error

    def test_zero_open_kernel_disables_it(self):
        assert DotDetectionSpec(open_kernel_px=0).validate() is None
        assert DotDetectionSpec(open_kernel_px=3).validate() is None


class TestDarkening:
    """塗布前後差分（明るくなった側は 0 に潰す）."""

    def test_reports_how_much_darker_post_is(self):
        pre = _gray(200)
        post = _gray(150)

        assert int(darkening(pre, post).max()) == 50

    def test_clips_brightening_to_zero(self):
        assert int(darkening(_gray(150), _gray(200)).max()) == 0


class TestBackgroundDarkening:
    """単一フレームの背景推定差分."""

    def test_keeps_dark_spot_and_removes_illumination_gradient(self):
        gradient = np.tile(
            np.linspace(120, 220, 200, dtype=np.uint8), (200, 1)
        )  # 左右で 100 階調の照明ムラ
        image = cv2.cvtColor(gradient, cv2.COLOR_GRAY2BGR)
        cv2.circle(image, (100, 100), 12, (40, 40, 40), -1)

        difference = background_darkening(image, kernel_px=41)

        assert int(difference[100, 100]) > 80
        # 斑点から離れた背景は、照明ムラがあっても 0 付近に落ちる
        assert int(difference[20, 20]) <= 2
        assert int(difference[20, 180]) <= 2

    def test_uniform_image_has_no_darkening(self):
        assert int(background_darkening(_gray(150), kernel_px=41).max()) == 0

    @pytest.mark.parametrize("kernel_px", [0, -1])
    def test_rejects_non_positive_kernel(self, kernel_px: int):
        with pytest.raises(ValueError):
            background_darkening(_gray(150), kernel_px=kernel_px)


class TestBackgroundKernelPx:
    """背景推定カーネルは想定する斑点より大きい正の奇数."""

    def test_exceeds_the_largest_expected_spot(self):
        assert background_kernel_px(20.0) >= 20

    @pytest.mark.parametrize("max_diameter_px", [0.0, 1.0, 20.0, 57.4])
    def test_is_positive_odd(self, max_diameter_px: float):
        kernel = background_kernel_px(max_diameter_px)

        assert kernel >= 3
        assert kernel % 2 == 1


class TestSegmentDarkening:
    """コントラストガード → Otsu（下限付き）→ open の段."""

    def test_blank_guard_reports_contrast_without_mask(self):
        difference = np.full((120, 120), 5, dtype=np.uint8)

        segmentation = segment_darkening(difference, DotDetectionSpec())

        assert segmentation.mask is None
        assert segmentation.contrast == pytest.approx(5.0)
        assert segmentation.threshold == pytest.approx(0.0)

    def test_thresholds_high_contrast_difference(self):
        difference = np.zeros((120, 120), dtype=np.uint8)
        cv2.circle(difference, (60, 60), 12, 120, -1)

        segmentation = segment_darkening(difference, DotDetectionSpec())

        assert segmentation.mask is not None
        assert segmentation.threshold > 0.0
        assert int(segmentation.mask[60, 60]) == 255
        assert int(segmentation.mask[10, 10]) == 0

    def test_threshold_never_falls_below_the_floor(self):
        difference = np.zeros((120, 120), dtype=np.uint8)
        cv2.circle(difference, (60, 60), 12, 120, -1)
        spec = DotDetectionSpec(min_contrast=100.0, threshold_floor_ratio=1.0)

        segmentation = segment_darkening(difference, spec)

        # Otsu が返す閾値（120 階調の斑点なので 100 未満）を下限が押し上げる
        assert segmentation.threshold == pytest.approx(100.0)
        assert segmentation.mask is not None

    def test_open_removes_isolated_speckles(self):
        difference = np.zeros((120, 120), dtype=np.uint8)
        cv2.circle(difference, (60, 60), 12, 120, -1)
        difference[10, 10] = 120

        opened = segment_darkening(difference, DotDetectionSpec(open_kernel_px=3))
        kept = segment_darkening(difference, DotDetectionSpec(open_kernel_px=0))

        assert opened.mask is not None and kept.mask is not None
        assert int(opened.mask[10, 10]) == 0
        assert int(kept.mask[10, 10]) == 255


class TestDarkSpots:
    """2 値マスクの連結成分（面積・重心・等価直径・円形度）."""

    def test_returns_empty_for_blank_mask(self):
        assert dark_spots(np.zeros((120, 120), dtype=np.uint8)) == ()

    def test_reports_area_center_and_equivalent_diameter(self):
        mask = _mask_with_disk((70, 40), 12)

        spots = dark_spots(mask)

        assert len(spots) == 1
        spot = spots[0]
        assert spot.area_px == pytest.approx(math.pi * 12**2, rel=0.05)
        assert spot.center.x == pytest.approx(70.0, abs=0.5)
        assert spot.center.y == pytest.approx(40.0, abs=0.5)
        assert spot.diameter_px == pytest.approx(24.0, abs=0.5)

    def test_reports_every_component(self):
        mask = _mask_with_disk((30, 30), 8)
        cv2.circle(mask, (90, 90), 12, 255, -1)

        spots = dark_spots(mask)

        assert len(spots) == 2
        assert {round(spot.center.x) for spot in spots} == {30, 90}

    def test_circularity_separates_disk_from_bar(self):
        disk = dark_spots(_mask_with_disk((60, 60), 10))[0]
        bar = np.zeros((120, 120), dtype=np.uint8)
        cv2.rectangle(bar, (40, 56), (79, 63), 255, -1)

        assert disk.circularity == pytest.approx(1.0, abs=0.1)
        assert dark_spots(bar)[0].circularity < 0.6

    def test_measures_a_spot_nested_in_another_spots_hole(self):
        """穴の中にある成分も自分の外周長を持つ（円形度 0 にならない）."""
        mask = np.zeros((120, 120), dtype=np.uint8)
        cv2.circle(mask, (60, 60), 30, 255, 4)  # 環
        cv2.circle(mask, (60, 60), 6, 255, -1)  # 環の内側の円

        spots = dark_spots(mask)

        nested = min(spots, key=lambda spot: spot.area_px)
        assert nested.perimeter_px > 0.0
        assert nested.circularity > 0.7


class TestFillDarkSpotHoles:
    """成分に囲まれた背景だけを前景へ変える."""

    def test_fills_an_enclosed_hole(self):
        mask = _mask_with_disk((60, 60), 20)
        cv2.circle(mask, (60, 60), 8, 0, -1)

        filled = fill_dark_spot_holes(mask)

        assert int(filled[60, 60]) == 255
        assert dark_spots(filled)[0].area_px > dark_spots(mask)[0].area_px

    def test_keeps_a_solid_spot_unchanged(self):
        mask = _mask_with_disk((60, 60), 20)

        assert np.array_equal(fill_dark_spot_holes(mask), mask)

    def test_leaves_background_open_to_the_border(self):
        """縁から届く背景（欠けた成分の内側）は埋めない."""
        mask = _mask_with_disk((60, 60), 20)
        mask[:, 55:65] = 0  # 上下の縁まで切り通す

        filled = fill_dark_spot_holes(mask)

        assert int(filled[60, 60]) == 0
        assert int(filled[0, 60]) == 0
