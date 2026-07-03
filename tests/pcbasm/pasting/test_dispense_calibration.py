"""dispense_calibration の算出モデル・段ずらし幾何のテスト.

別 agent が `pcbasm.pasting.__init__` を編集中の可能性があるため、干渉を避けて
モジュールを直接 import する（自分のロジック検証に集中する方針）。
"""

import math
from typing import Any

import pytest

from pcbasm.geometry import Point2d
from pcbasm.pasting.dispense_calibration import (
    DispenseRateCalibration,
    FillSpeedSweep,
    LineLayout,
    LineLayoutOverflowError,
    RateMeasurement,
    RotationsPerUlRound,
    dispense_rate_schedule,
    fill_speed_schedule,
    rate_sweep_amount,
    slot_area,
)


class TestSlotArea:
    """slot_area 純粋関数のテスト."""

    def test_rectangle_plus_circle_cap(self):
        # length=10, bead_width=0.4 → 10*0.4 + π*0.2² = 4 + π*0.04
        area = slot_area(10.0, 0.4)
        assert area == pytest.approx(10.0 * 0.4 + math.pi * 0.2**2)

    def test_zero_length_is_full_circle(self):
        # length=0 なら両端キャップ（1円）だけが残る
        area = slot_area(0.0, 0.6)
        assert area == pytest.approx(math.pi * 0.3**2)


class TestRateSweepAmount:
    """rate_sweep_amount 純粋関数のテスト.

    FillSequence の速度モデル（rate = amount × speed / 経路長、rate_cap は
    頭打ちのみ）に対し、移動速度を変えずに指令レートを実現する吐出量の導出。
    """

    def test_amount_scales_linearly_with_rate(self):
        # rate=0.5, L=10, v=0.8 → 0.5*10/0.8 = 6.25 uL
        assert rate_sweep_amount(0.5, 10.0, 0.8) == pytest.approx(6.25)
        assert rate_sweep_amount(1.0, 10.0, 0.8) == pytest.approx(12.5)

    def test_amount_reproduces_fill_sequence_rate_derivation(self):
        # FillSequence の導出 r = amount × v / L に代入すると指令レートへ戻る
        rate, length, speed = 3.0, 12.0, 1.5
        amount = rate_sweep_amount(rate, length, speed)
        assert amount * speed / length == pytest.approx(rate)


class TestLineLayout:
    """LineLayout 段ずらし・折り返し幾何のテスト.

    既定は銅板 40×40 mm・マージン 5 mm（描画領域 30×30 mm）・線長 10 mm。 row_pitch=3 のとき 1
    列 11 本 × 2 列 = 容量 22 本になる。
    """

    @staticmethod
    def _layout(**kwargs: Any) -> LineLayout:
        defaults: dict[str, Any] = dict(
            line_length=10.0,
            line_count=1,
            row_pitch=2.0,
            board_width=40.0,
            board_height=40.0,
            margin=5.0,
        )
        defaults.update(kwargs)
        return LineLayout(**defaults)

    def test_single_line_starts_at_margin(self):
        layout = self._layout(line_count=1)
        start, end = layout.line(0)
        assert start == Point2d(5.0, 5.0)
        assert end == Point2d(15.0, 5.0)

    def test_lines_are_staggered_by_row_pitch_within_column(self):
        layout = self._layout(line_count=3)
        lines = layout.lines
        assert len(lines) == 3
        # 各線は X 方向に伸び、Y は margin + index*row_pitch でずれる
        for i, (start, end) in enumerate(lines):
            assert start == Point2d(5.0, 5.0 + i * 2.0)
            assert end == Point2d(15.0, 5.0 + i * 2.0)

    def test_margin_defaults_to_zero(self):
        layout = LineLayout(
            line_length=10.0,
            line_count=1,
            row_pitch=2.0,
            board_width=10.0,
            board_height=1.0,
        )
        assert layout.line(0) == (Point2d(0.0, 0.0), Point2d(10.0, 0.0))

    def test_rows_per_column_includes_exact_fit_bottom_row(self):
        # 描画領域高さ 30 / pitch 3 → 0,3,…,30 の 11 本（下端ちょうども含む）
        assert self._layout(row_pitch=3.0).rows_per_column == 11

    def test_wraps_to_next_column_at_board_bottom(self):
        layout = self._layout(line_count=12, row_pitch=3.0)
        # 11 本目は列の下端（y = 40 - 5 = 35）ちょうど
        assert layout.line(10)[0] == Point2d(5.0, 35.0)
        # 12 本目は右隣の列（x += line_length + row_pitch = 13）の先頭へ折り返す
        assert layout.line(11) == (Point2d(18.0, 5.0), Point2d(28.0, 5.0))

    def test_capacity_is_rows_times_columns(self):
        layout = self._layout(row_pitch=3.0)
        # 幅 30 に線長 10 の列が column_pitch=13 で 2 列 → 11 × 2 = 22
        assert layout.max_columns == 2
        assert layout.capacity == 22

    def test_full_capacity_layout_stays_within_drawing_area(self):
        layout = self._layout(line_count=22, row_pitch=3.0)
        for start, end in layout.lines:
            for point in (start, end):
                assert 5.0 <= point.x <= 35.0
                assert 5.0 <= point.y <= 35.0

    def test_overflow_raises_with_capacity(self):
        with pytest.raises(LineLayoutOverflowError) as excinfo:
            self._layout(line_count=23, row_pitch=3.0)
        assert excinfo.value.line_count == 23
        assert excinfo.value.capacity == 22

    def test_line_longer_than_drawing_area_has_zero_capacity(self):
        # 線長 31 > 描画領域幅 30 → 1 本も置けない
        with pytest.raises(LineLayoutOverflowError) as excinfo:
            self._layout(line_length=31.0)
        assert excinfo.value.capacity == 0

    def test_margin_exceeding_board_has_zero_capacity(self):
        with pytest.raises(LineLayoutOverflowError):
            self._layout(board_height=8.0)  # 2*margin=10 > 8

    def test_line_index_out_of_range_raises(self):
        layout = self._layout(line_count=2)
        with pytest.raises(IndexError):
            layout.line(2)
        with pytest.raises(IndexError):
            layout.line(-1)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("line_length", 0.0),
            ("line_count", 0),
            ("row_pitch", 0.0),
            ("board_width", 0.0),
            ("board_height", 0.0),
            ("margin", -1.0),
        ],
    )
    def test_invalid_construction_raises(self, field, value):
        with pytest.raises(ValueError):
            self._layout(**{field: value})


class TestDispenseRateSchedule:
    """dispense_rate_schedule のテスト."""

    def test_evenly_spaced_ascending(self):
        schedule = dispense_rate_schedule(1.0, 5.0, 5)
        assert schedule == pytest.approx([1.0, 2.0, 3.0, 4.0, 5.0])

    def test_single_division_returns_min(self):
        assert dispense_rate_schedule(2.0, 8.0, 1) == [2.0]

    @pytest.mark.parametrize(
        ("rate_min", "rate_max", "divisions"),
        [(0.0, 5.0, 3), (-1.0, 5.0, 3), (5.0, 1.0, 3), (1.0, 5.0, 0)],
    )
    def test_invalid_input_returns_empty(self, rate_min, rate_max, divisions):
        assert dispense_rate_schedule(rate_min, rate_max, divisions) == []


class TestFillSpeedSchedule:
    """fill_speed_schedule のテスト."""

    def test_evenly_spaced(self):
        schedule = fill_speed_schedule(10.0, 30.0, 5)
        assert schedule == pytest.approx([10.0, 15.0, 20.0, 25.0, 30.0])

    def test_single_division_returns_min(self):
        assert fill_speed_schedule(12.0, 40.0, 1) == [12.0]

    @pytest.mark.parametrize(
        ("speed_min", "speed_max", "divisions"),
        [(0.0, 30.0, 3), (30.0, 10.0, 3), (10.0, 30.0, 0)],
    )
    def test_invalid_input_returns_empty(self, speed_min, speed_max, divisions):
        assert fill_speed_schedule(speed_min, speed_max, divisions) == []


class TestRateMeasurement:
    """RateMeasurement のテスト."""

    def test_efficiency_is_measured_over_commanded(self):
        m = RateMeasurement(rate=1.0, measured_ul=9.0, commanded_ul=10.0)
        assert m.efficiency == pytest.approx(0.9)


class TestDispenseRateCalibration:
    """DispenseRateCalibration 効率落ち検出のテスト."""

    @staticmethod
    def _calib(efficiencies, **kwargs):
        # commanded_ul=10 固定で、欲しい efficiency になる measured_ul を与える
        measurements = [
            RateMeasurement(
                rate=float(i + 1), measured_ul=eff * 10.0, commanded_ul=10.0
            )
            for i, eff in enumerate(efficiencies)
        ]
        return DispenseRateCalibration(measurements=measurements, **kwargs)

    def test_efficiencies_property(self):
        calib = self._calib([1.0, 0.98, 0.95])
        assert calib.efficiencies == pytest.approx((1.0, 0.98, 0.95))

    def test_baseline_is_median_of_low_rate_points(self):
        calib = self._calib([1.0, 0.9, 0.95, 0.5], baseline_count=3)
        # 低レート 3 点 [1.0, 0.9, 0.95] の中央値 = 0.95
        assert calib.baseline_efficiency == pytest.approx(0.95)

    def test_detects_drop_returns_rate_before_drop(self):
        # baseline ~1.0、4 点目(rate=4)で 10% 超落ち → 直前 rate=3 が上限
        calib = self._calib([1.0, 1.0, 1.0, 0.85], drop_frac=0.10)
        assert calib.max_dispense_rate == pytest.approx(3.0)

    def test_monotonic_no_drop_returns_none(self):
        # 全域で baseline から 10% 以内 → 判定不能（落ちが無い）
        calib = self._calib([1.0, 0.98, 0.96, 0.95], drop_frac=0.10)
        assert calib.max_dispense_rate is None

    def test_all_good_high_efficiency_returns_none(self):
        calib = self._calib([1.0, 1.0, 1.0, 1.0])
        assert calib.max_dispense_rate is None

    def test_drop_at_first_point_returns_none(self):
        # baseline=median([0.5,1.0])=0.75, threshold=0.675。1 点目(0.5)が既に閾値割れ
        # → 直前のレートが存在しないため None
        calib = self._calib([0.5, 1.0], baseline_count=2, drop_frac=0.10)
        assert calib.max_dispense_rate is None

    def test_noisy_but_within_tolerance_returns_none(self):
        # baseline 0.97 付近、ノイズで上下するが 10% 落ちは無い
        calib = self._calib([0.95, 1.0, 0.97, 0.99, 0.96], drop_frac=0.10)
        assert calib.max_dispense_rate is None

    def test_noisy_with_real_drop_detected(self):
        # baseline 中央値 ~0.97、最後で 0.80 に落ちる → 直前 rate=4
        calib = self._calib([0.95, 1.0, 0.97, 0.99, 0.80], drop_frac=0.10)
        assert calib.max_dispense_rate == pytest.approx(4.0)

    @pytest.mark.parametrize("drop_frac", [0.0, 1.0, 1.5, -0.1])
    def test_invalid_drop_frac_raises(self, drop_frac):
        with pytest.raises(ValueError):
            self._calib([1.0, 0.9], drop_frac=drop_frac)

    def test_empty_measurements_raises(self):
        with pytest.raises(ValueError):
            DispenseRateCalibration(measurements=[])


class TestFillSpeedSweep:
    """FillSpeedSweep のテスト."""

    def test_speed_at_returns_selected_speed(self):
        sweep = FillSpeedSweep(speeds=[10.0, 20.0, 30.0])
        assert sweep.speed_at(0) == 10.0
        assert sweep.speed_at(2) == 30.0

    @pytest.mark.parametrize("index", [-1, 3, 100])
    def test_speed_at_out_of_range_returns_none(self, index):
        sweep = FillSpeedSweep(speeds=[10.0, 20.0, 30.0])
        assert sweep.speed_at(index) is None

    def test_empty_speeds_raises(self):
        with pytest.raises(ValueError):
            FillSpeedSweep(speeds=[])


class TestRotationsPerUlRound:
    """RotationsPerUlRound 収束判定のテスト."""

    def test_relative_change(self):
        round_ = RotationsPerUlRound(previous=2.0, computed=2.2)
        assert round_.relative_change == pytest.approx(0.1)

    def test_converged_within_tolerance(self):
        round_ = RotationsPerUlRound(previous=2.0, computed=2.02)
        assert round_.converged(rel_tol=0.05) is True

    def test_not_converged_outside_tolerance(self):
        round_ = RotationsPerUlRound(previous=2.0, computed=2.5)
        assert round_.converged(rel_tol=0.05) is False

    def test_converged_at_tolerance_boundary_inclusive(self):
        # relative_change をそのまま rel_tol に渡せば境界は inclusive(<=)
        round_ = RotationsPerUlRound(previous=2.0, computed=2.1)
        assert round_.converged(rel_tol=round_.relative_change) is True

    @pytest.mark.parametrize(
        ("previous", "computed"),
        [(0.0, 2.0), (-1.0, 2.0), (2.0, 0.0), (2.0, -1.0)],
    )
    def test_invalid_construction_raises(self, previous, computed):
        with pytest.raises(ValueError):
            RotationsPerUlRound(previous=previous, computed=computed)
