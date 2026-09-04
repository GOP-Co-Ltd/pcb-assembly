"""flowcalib.lines（段ずらしレイアウトと掃引点の計画）のテスト."""

from typing import Any

import pytest

from pcbasm.geometry import Point2d
from pcbasm.pasting.flowcalib.flow import slot_area
from pcbasm.pasting.flowcalib.lines import (
    LineLayout,
    plan_rate_sweep,
    plan_speed_sweep,
)
from pcbasm.pasting.flowcalib.params import CalibrationParams


def _layout(**kwargs: Any) -> LineLayout:
    """既定は銅板 40×40 mm・マージン 5 mm（描画領域 30×30 mm）・線長 10 mm."""
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


class TestLineLayout:
    """段ずらし・折り返し幾何。row_pitch=3 のとき 1 列 11 本 × 2 列 = 容量 22 本."""

    def test_single_line_starts_at_margin(self):
        start, end = _layout(line_count=1).line(0)
        assert start == Point2d(5.0, 5.0)
        assert end == Point2d(15.0, 5.0)

    def test_lines_are_staggered_by_row_pitch_within_column(self):
        lines = _layout(line_count=3).lines
        assert len(lines) == 3
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
        assert _layout(row_pitch=3.0).rows_per_column == 11

    def test_wraps_to_next_column_at_board_bottom(self):
        layout = _layout(line_count=12, row_pitch=3.0)
        # 11 本目は列の下端（y = 40 - 5 = 35）ちょうど
        assert layout.line(10)[0] == Point2d(5.0, 35.0)
        # 12 本目は右隣の列（x += line_length + row_pitch = 13）の先頭へ折り返す
        assert layout.line(11) == (Point2d(18.0, 5.0), Point2d(28.0, 5.0))

    def test_capacity_is_rows_times_columns(self):
        layout = _layout(row_pitch=3.0)
        # 幅 30 に線長 10 の列が column_pitch=13 で 2 列 → 11 × 2 = 22
        assert layout.max_columns == 2
        assert layout.capacity == 22

    def test_full_capacity_layout_stays_within_drawing_area(self):
        layout = _layout(line_count=22, row_pitch=3.0)
        assert layout.fits
        for start, end in layout.lines:
            for point in (start, end):
                assert 5.0 <= point.x <= 35.0
                assert 5.0 <= point.y <= 35.0

    @pytest.mark.parametrize(
        ("kwargs", "capacity"),
        [
            (dict(line_count=23, row_pitch=3.0), 22),
            (dict(line_length=31.0), 0),  # 線長 31 > 描画領域幅 30
            (dict(board_height=8.0), 0),  # 2*margin=10 > 8
        ],
    )
    def test_overflow_does_not_fit_but_still_constructs(self, kwargs, capacity):
        layout = _layout(**kwargs)

        assert layout.fits is False
        assert layout.capacity == capacity

    def test_line_index_out_of_range_raises(self):
        layout = _layout(line_count=2)
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
        with pytest.raises(ValueError):  # noqa: PT011 - attrs の詳細文言は固定しない
            _layout(**{field: value})


class TestLineLayoutValidate:
    def test_fitting_layout_is_valid(self):
        assert _layout(line_count=22, row_pitch=3.0).validate() is None

    def test_overflow_message_names_requested_and_capacity(self):
        message = _layout(line_count=23, row_pitch=3.0).validate()

        assert message is not None
        assert "線 23 本" in message
        assert "最大 22 本" in message
        assert "調整してください" in message


class TestPlanRateSweep:
    """② レート掃引点（専用の線位置付き）."""

    def test_points_follow_schedule_and_layout(self):
        params = CalibrationParams(
            rate_min=1.0, rate_max=3.0, rate_divisions=3, line_length=10.0
        )

        points, message = plan_rate_sweep(params, fill_speed=2.0)

        assert message is None
        assert points is not None
        assert [p.index for p in points] == [0, 1, 2]
        assert [p.rate for p in points] == pytest.approx([1.0, 2.0, 3.0])
        # 吐出量 = rate × 線長 / 速度
        assert [p.amount_ul for p in points] == pytest.approx([5.0, 10.0, 15.0])
        layout = params.line_layout(3)
        assert [(p.start, p.end) for p in points] == list(layout.lines)

    def test_uses_divisions_not_line_count_for_layout(self):
        params = CalibrationParams(line_count=1, rate_divisions=4)

        points, _ = plan_rate_sweep(params, fill_speed=2.0)

        assert points is not None
        assert len(points) == 4
        assert len({p.start for p in points}) == 4

    def test_empty_schedule_reports_reason(self):
        params = CalibrationParams(rate_min=5.0, rate_max=1.0)

        points, message = plan_rate_sweep(params, fill_speed=2.0)

        assert points is None
        assert message is not None
        assert "吐出レート列" in message

    def test_layout_overflow_reports_capacity(self):
        params = CalibrationParams(rate_divisions=23, row_pitch=3.0)

        points, message = plan_rate_sweep(params, fill_speed=2.0)

        assert points is None
        assert message is not None
        assert "最大 22 本" in message


class TestPlanSpeedSweep:
    """③ 速度掃引点（実塗布同等の総量で固定）."""

    def test_points_follow_schedule_and_share_amount(self):
        params = CalibrationParams(
            speed_min=1.0, speed_max=5.0, speed_divisions=5, line_length=10.0
        )

        sweep, message = plan_speed_sweep(params, ul_per_mm2=0.05, bead_width=0.4)

        assert message is None
        assert sweep is not None
        assert [p.fill_speed for p in sweep.points] == pytest.approx(
            [1.0, 2.0, 3.0, 4.0, 5.0]
        )
        assert sweep.total_amount_ul == pytest.approx(0.05 * slot_area(10.0, 0.4))
        assert all(p.amount_ul == sweep.total_amount_ul for p in sweep.points)
        assert [(p.start, p.end) for p in sweep.points] == list(
            params.line_layout(5).lines
        )

    def test_empty_schedule_reports_reason(self):
        params = CalibrationParams(speed_min=0.0)

        sweep, message = plan_speed_sweep(params, ul_per_mm2=0.05, bead_width=0.4)

        assert sweep is None
        assert message is not None
        assert "塗布速度列" in message

    def test_layout_overflow_reports_capacity(self):
        params = CalibrationParams(speed_divisions=23, row_pitch=3.0)

        sweep, message = plan_speed_sweep(params, ul_per_mm2=0.05, bead_width=0.4)

        assert sweep is None
        assert message is not None
        assert "最大 22 本" in message
