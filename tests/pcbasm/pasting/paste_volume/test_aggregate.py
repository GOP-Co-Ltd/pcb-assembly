"""複数 view の直径を 1 つへ畳む契約.

マルチ view の役割は検出失敗時のフォールバック。1 view が落ちても残りで測れること、 集約が中央値であること（3
次モデルとの可換性の前提）を固める。
"""

import pytest

from pcbasm.pasting.paste_volume.aggregate import DotDiameter, aggregate_views
from pcbasm.pasting.paste_volume.detect import DotMeasurement


def _measured(diameter_mm: float) -> DotMeasurement:
    detected = diameter_mm > 0.0
    return DotMeasurement(
        diameter_mm=diameter_mm,
        area_px=10 if detected else 0,
        contrast=90.0 if detected else 2.0,
        threshold=45.0 if detected else 0.0,
        detected=detected,
    )


class TestAggregateViews:
    """検出できた view の中央値を採る."""

    def test_single_view_passes_through(self):
        result = aggregate_views([_measured(0.8)])

        assert result.diameter_mm == pytest.approx(0.8)
        assert result.view_count == 1
        assert result.detected_view_count == 1

    def test_uses_the_median_of_detected_views(self):
        result = aggregate_views([_measured(0.7), _measured(0.8), _measured(0.9)])

        assert result.diameter_mm == pytest.approx(0.8)

    def test_even_count_averages_the_two_middle_views(self):
        result = aggregate_views([_measured(0.6), _measured(0.8)])

        assert result.diameter_mm == pytest.approx(0.7)

    def test_an_outlier_view_does_not_drag_the_median(self):
        """中央値を採る理由。1 view が大きく外れても結果が引きずられない."""
        result = aggregate_views(
            [_measured(0.80), _measured(0.81), _measured(0.82), _measured(5.0)]
        )

        assert result.diameter_mm == pytest.approx(0.815)


class TestAggregateViewsFallback:
    """検出失敗は除外して残りで測る（マルチ view の主目的）."""

    def test_undetected_views_are_excluded_from_the_median(self):
        result = aggregate_views(
            [_measured(0.0), _measured(0.8), _measured(0.8), _measured(0.0)]
        )

        assert result.diameter_mm == pytest.approx(0.8)
        assert result.view_count == 4
        assert result.detected_view_count == 2

    def test_one_surviving_view_still_measures(self):
        result = aggregate_views([_measured(0.0), _measured(0.0), _measured(0.9)])

        assert result.diameter_mm == pytest.approx(0.9)
        assert result.detected_view_count == 1

    def test_all_views_undetected_means_zero(self):
        """Blank セルの真値 0。検出失敗と同じ形で表す."""
        result = aggregate_views([_measured(0.0), _measured(0.0)])

        assert result.diameter_mm == 0.0
        assert result.detected_view_count == 0

    def test_no_views_at_all_means_zero(self):
        result = aggregate_views([])

        assert result.diameter_mm == 0.0
        assert result.view_count == 0
        assert result.detected_view_count == 0


class TestDotDiameterDiagnostics:
    """フォールバックの品質を後から見るための診断値."""

    def test_spread_reports_the_range_of_detected_views(self):
        result = aggregate_views([_measured(0.80), _measured(0.86), _measured(0.0)])

        assert result.spread_mm == pytest.approx(0.06)

    def test_spread_is_zero_without_detected_views(self):
        assert aggregate_views([_measured(0.0)]).spread_mm == 0.0

    def test_keeps_every_view_diameter_in_order(self):
        result = aggregate_views([_measured(0.8), _measured(0.0), _measured(0.9)])

        assert result.view_diameters_mm == (0.8, 0.0, 0.9)
