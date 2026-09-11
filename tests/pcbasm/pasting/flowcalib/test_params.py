"""CalibrationParams（ジョブ param の読み直しとレイアウト構築）のテスト."""

import pytest

from pcbasm.pasting.flowcalib.params import CalibrationParams


class TestCalibrationParams:
    """from_mapping の正規化・既定値・required_line_count・line_layout."""

    def test_missing_keys_fall_back_to_defaults(self):
        params = CalibrationParams.from_mapping({"line_length": 12.0})

        assert params.line_length == 12.0
        assert params == CalibrationParams(line_length=12.0)

    @pytest.mark.parametrize(
        ("name", "value", "expected"),
        [
            ("line_count", 0, 1),
            ("line_count", -3, 1),
            ("rate_divisions", 4.0, 4),
            ("speed_divisions", 7, 7),
        ],
    )
    def test_int_fields_are_clamped_to_at_least_one(self, name, value, expected):
        params = CalibrationParams.from_mapping({name: value})

        assert getattr(params, name) == expected

    def test_line_amount_param_name_is_read_as_line_amount_ul(self):
        params = CalibrationParams.from_mapping({"line_amount": 0.75})

        assert params.line_amount_ul == 0.75

    def test_unknown_keys_are_ignored(self):
        params = CalibrationParams.from_mapping({"tolerance": 0.1, "board_width": 50.0})

        assert params.board_width == 50.0

    @pytest.mark.parametrize(
        ("line_count", "rate_divisions", "speed_divisions", "expected"),
        [(10, 6, 6, 10), (3, 8, 6, 8), (3, 4, 12, 12)],
    )
    def test_required_line_count_is_the_maximum(
        self, line_count, rate_divisions, speed_divisions, expected
    ):
        params = CalibrationParams(
            line_count=line_count,
            rate_divisions=rate_divisions,
            speed_divisions=speed_divisions,
        )

        assert params.required_line_count == expected

    def test_line_layout_line_count_override(self):
        layout = CalibrationParams(line_count=4).line_layout(6)

        assert layout.line_count == 6
