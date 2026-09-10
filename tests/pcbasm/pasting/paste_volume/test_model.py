"""直径 → 体積の 3 次モデルと、そのフィットの公開契約.

切片を 0 に固定する（``V = a·d³ + b·d² + c·d``）。blank の真値 0 を厳密に表せる
必要があるため、定数項は持たない。
"""

import math

import attrs
import pytest

from pcbasm.pasting.paste_volume.model import (
    MODEL_KIND,
    CubicVolumeModel,
    fit_cubic_through_origin,
)

# 検証用の既知係数（半球に近い形）
KNOWN = CubicVolumeModel(
    cubic_ul_per_mm3=0.24,
    quadratic_ul_per_mm2=-0.046,
    linear_ul_per_mm=0.004,
    diameter_min_mm=0.5,
    diameter_max_mm=1.2,
)


def _samples(
    model: CubicVolumeModel, count: int = 12
) -> tuple[list[float], list[float]]:
    step = (model.diameter_max_mm - model.diameter_min_mm) / (count - 1)
    diameters = [model.diameter_min_mm + step * index for index in range(count)]
    return diameters, [model.volume_ul(diameter) for diameter in diameters]


class TestCubicVolumeModel:
    """モデルの評価と被覆域."""

    def test_kind_is_recorded_for_the_calibration_file(self):
        assert MODEL_KIND == "cubic_through_origin"

    def test_zero_diameter_is_exactly_zero_volume(self):
        assert KNOWN.volume_ul(0.0) == 0.0

    def test_negative_diameter_is_zero_volume(self):
        assert KNOWN.volume_ul(-1.0) == 0.0

    def test_evaluates_the_cubic_polynomial(self):
        diameter = 0.8
        expected = (
            KNOWN.cubic_ul_per_mm3 * diameter**3
            + KNOWN.quadratic_ul_per_mm2 * diameter**2
            + KNOWN.linear_ul_per_mm * diameter
        )

        assert KNOWN.volume_ul(diameter) == pytest.approx(expected)

    @pytest.mark.parametrize(
        ("diameter", "covered"),
        [(0.5, True), (0.85, True), (1.2, True), (0.49, False), (1.21, False)],
    )
    def test_covers_reports_the_fitted_range(self, diameter: float, covered: bool):
        assert KNOWN.covers(diameter) is covered

    def test_validate_accepts_a_sane_model(self):
        assert KNOWN.validate() is None

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("cubic_ul_per_mm3", float("nan")),
            ("quadratic_ul_per_mm2", float("inf")),
            ("linear_ul_per_mm", float("nan")),
            ("diameter_min_mm", -0.1),
            ("diameter_max_mm", float("nan")),
        ],
    )
    def test_validate_rejects_non_finite_or_negative(self, field: str, value: float):
        error = attrs.evolve(KNOWN, **{field: value}).validate()

        assert error is not None

    def test_validate_rejects_an_inverted_range(self):
        error = attrs.evolve(KNOWN, diameter_min_mm=1.5).validate()

        assert error is not None


class TestFitCubicThroughOrigin:
    """最小二乗フィット（切片なし）."""

    def test_recovers_known_coefficients(self):
        diameters, volumes = _samples(KNOWN)

        model, error = fit_cubic_through_origin(diameters, volumes)

        assert error is None, error
        assert model is not None
        assert model.cubic_ul_per_mm3 == pytest.approx(KNOWN.cubic_ul_per_mm3, rel=1e-6)
        assert model.quadratic_ul_per_mm2 == pytest.approx(
            KNOWN.quadratic_ul_per_mm2, rel=1e-6
        )
        assert model.linear_ul_per_mm == pytest.approx(KNOWN.linear_ul_per_mm, rel=1e-6)

    def test_records_the_fitted_diameter_range(self):
        diameters, volumes = _samples(KNOWN)

        model, _ = fit_cubic_through_origin(diameters, volumes)

        assert model is not None
        assert model.diameter_min_mm == pytest.approx(min(diameters))
        assert model.diameter_max_mm == pytest.approx(max(diameters))

    def test_blank_rows_do_not_change_the_fit(self):
        """(0, 0) は設計行列上ゼロ行なので、入れても結果が変わらない."""
        diameters, volumes = _samples(KNOWN)
        with_blanks = ([*diameters, 0.0, 0.0], [*volumes, 0.0, 0.0])

        plain, _ = fit_cubic_through_origin(diameters, volumes)
        blanked, _ = fit_cubic_through_origin(*with_blanks)

        assert plain is not None and blanked is not None
        assert blanked.cubic_ul_per_mm3 == pytest.approx(plain.cubic_ul_per_mm3)
        # blank は被覆域も広げない（正の直径だけが範囲を決める）
        assert blanked.diameter_min_mm == pytest.approx(plain.diameter_min_mm)

    def test_fits_noisy_data_without_blowing_up(self):
        diameters, volumes = _samples(KNOWN, count=40)
        noisy = [
            volume * (1.0 + 0.02 * math.sin(index))
            for index, volume in enumerate(volumes)
        ]

        model, error = fit_cubic_through_origin(diameters, noisy)

        assert error is None, error
        assert model is not None
        assert model.volume_ul(0.9) == pytest.approx(KNOWN.volume_ul(0.9), rel=0.05)


class TestFitRejections:
    """フィットできない入力は理由を返す（例外にしない）."""

    def test_rejects_mismatched_lengths(self):
        model, error = fit_cubic_through_origin([0.5, 0.6], [0.1])

        assert model is None
        assert error is not None

    def test_rejects_fewer_than_three_positive_diameters(self):
        model, error = fit_cubic_through_origin([0.5, 0.6, 0.0], [0.1, 0.2, 0.0])

        assert model is None
        assert error is not None

    def test_rejects_a_single_repeated_diameter(self):
        model, error = fit_cubic_through_origin([0.8] * 5, [0.2] * 5)

        assert model is None
        assert error is not None

    def test_rejects_non_finite_values(self):
        model, error = fit_cubic_through_origin(
            [0.5, 0.6, float("nan")], [0.1, 0.2, 0.3]
        )

        assert model is None
        assert error is not None

    def test_rejects_negative_volumes(self):
        model, error = fit_cubic_through_origin([0.5, 0.6, 0.7], [0.1, -0.2, 0.3])

        assert model is None
        assert error is not None

    def test_rejects_a_fit_that_goes_non_positive_inside_its_range(self):
        """被覆域内で体積が 0 以下になる係数は物理的にありえない."""
        diameters = [1.034, 1.089, 0.507, 0.817, 0.921]
        volumes = [0.174, 0.436, 0.139, 0.009, 0.02]

        model, error = fit_cubic_through_origin(diameters, volumes)

        assert model is None
        assert error is not None
        assert "0以下" in error


class TestMonotonicity:
    """中央値集約と可換であるための前提（被覆域内で単調増加）."""

    def test_reports_monotonicity_inside_the_fitted_range(self):
        diameters, volumes = _samples(KNOWN)

        model, _ = fit_cubic_through_origin(diameters, volumes)

        assert model is not None
        assert model.is_monotonic_in_range() is True

    def test_reports_a_fit_that_is_not_monotonic_inside_its_range(self):
        """単調性は当てはめた結果であって、常に True ではない."""
        diameters = [1.076, 1.006, 0.736, 0.607, 0.809]
        volumes = [0.202, 0.392, 0.152, 0.238, 0.292]

        model, error = fit_cubic_through_origin(diameters, volumes)

        assert error is None, error
        assert model is not None
        assert model.is_monotonic_in_range() is False

    def test_median_of_volumes_equals_volume_of_median_when_monotonic(self):
        """``median(V(dᵢ)) == V(median(dᵢ))``。単一 view 契約の上で集約できる根拠."""
        diameters, volumes = _samples(KNOWN)
        model, _ = fit_cubic_through_origin(diameters, volumes)
        assert model is not None

        views = [0.70, 0.82, 0.95]
        median_of_volumes = sorted(model.volume_ul(d) for d in views)[1]
        volume_of_median = model.volume_ul(sorted(views)[1])

        assert median_of_volumes == pytest.approx(volume_of_median)

    def test_an_even_view_count_only_matches_approximately(self):
        """偶数 view の中央値は中央 2 つの平均なので、2 次以上のぶんだけずれる.

        1 view が検出できずに落ちて偶数になる経路が実在する。

        そこで厳密一致ではなく「実害の無い微小差」であることを固定する。
        """
        diameters, volumes = _samples(KNOWN)
        model, _ = fit_cubic_through_origin(diameters, volumes)
        assert model is not None

        # 実素材の 2 view 間ばらつきと同じ水準（相対 0.2% 程度）
        views = [0.8638, 0.8656]
        median_of_volumes = sum(model.volume_ul(d) for d in views) / 2.0
        volume_of_median = model.volume_ul(sum(views) / 2.0)

        assert median_of_volumes != volume_of_median
        assert median_of_volumes == pytest.approx(volume_of_median, rel=1e-4)
