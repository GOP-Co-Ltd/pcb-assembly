"""掃引点列の等間隔分割（pcbasm.pasting.sweep）の公開契約."""

import pytest

from pcbasm.pasting.sweep import sweep_schedule


class TestSweepSchedule:
    """流量キャリブレーションの掃引と dataset の吐出量列が共用する等間隔昇順列."""

    def test_evenly_spaced_ascending(self):
        assert sweep_schedule(1.0, 5.0, 5) == pytest.approx((1.0, 2.0, 3.0, 4.0, 5.0))

    def test_single_division_returns_minimum(self):
        assert sweep_schedule(2.0, 8.0, 1) == (2.0,)

    @pytest.mark.parametrize(
        ("minimum", "maximum", "divisions"),
        [(0.0, 5.0, 3), (-1.0, 5.0, 3), (5.0, 1.0, 3), (1.0, 5.0, 0)],
    )
    def test_invalid_input_returns_empty(self, minimum, maximum, divisions):
        assert sweep_schedule(minimum, maximum, divisions) == ()
