"""`pcbasm.pasting.labels` の仕様テスト（表示文字列はサーバー側で組む）."""

from pcbasm.geometry import Point2d, Point3d
from pcbasm.pasting.labels import (
    clean_position_label,
    duration_label,
    flow_calibration_label,
    fmt_xy,
    fmt_xyz,
    purge_selection_label,
    tact_breakdown_label,
)
from pcbasm.pasting.tact import TactEstimate


class TestCoordinates:
    def test_xy_defaults_to_two_decimals(self):
        assert fmt_xy(Point2d(1.5, -2.25)) == "(1.50, -2.25)"

    def test_digits_are_selectable(self):
        assert fmt_xy(Point2d(1.0, 2.0), 3) == "(1.000, 2.000)"
        assert fmt_xyz(Point3d(1.0, 2.0, -3.0)) == "(1.00, 2.00, -3.00)"


class TestCleanPositionLabel:
    def test_label_shows_the_recorded_surface_position(self):
        """押し込み量は設定欄で編集するので含めない（含めると表示だけ古くなる）."""
        assert clean_position_label(Point3d(68.0, 53.0, -3.0)) == (
            "(68.00, 53.00, -3.00) mm"
        )


class TestPurgeSelectionLabel:
    def test_explicit_point(self):
        assert purge_selection_label(Point2d(1.0, 2.0), None) == "(1.00, 2.00) mm"

    def test_automatic_point(self):
        assert purge_selection_label(None, Point2d(1.0, 2.0)) == "自動 (1.00, 2.00) mm"

    def test_empty_route(self):
        assert purge_selection_label(None, None) == "自動 (順路が空です)"


class TestFlowCalibrationLabel:
    POINTS = (Point2d(10.0, 6.0), Point2d(13.0, 6.0))

    def test_unset(self):
        assert (
            flow_calibration_label((), planned_amount_ul=None, error=None)
            == "未設定（補正しません）"
        )

    def test_planned(self):
        assert (
            flow_calibration_label(self.POINTS, planned_amount_ul=0.2, error=None)
            == "2 点 (10.00, 6.00)、(13.00, 6.00) mm x 0.200 uL"
        )

    def test_the_reason_is_shown_when_not_planned(self):
        label = flow_calibration_label(
            self.POINTS, planned_amount_ul=None, error="撮影範囲が重なっています"
        )

        assert label.endswith("（撮影範囲が重なっています）")

    def test_a_missing_calibration_file_is_the_reason_without_an_error(self):
        label = flow_calibration_label(self.POINTS, planned_amount_ul=None, error=None)

        assert "校正ファイル未設定" in label


class TestDurationLabel:
    def test_hours_minutes_and_seconds(self):
        assert duration_label(3725.9) == "1時間2分5秒"

    def test_short_duration_keeps_zero_hours(self):
        assert duration_label(59.0) == "0時間0分59秒"


class TestTactBreakdownLabel:
    def test_pad_count_setup_and_paste(self):
        estimate = TactEstimate(setup_seconds=300.0, paste_seconds=95.0, pad_count=24)

        assert tact_breakdown_label(estimate) == (
            "24 pad（前段 0時間5分0秒 + 塗布 0時間1分35秒）"
        )
