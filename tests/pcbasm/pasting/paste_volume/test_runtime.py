"""運転時流量キャリブレーションの計画と補正の公開契約.

装置に触れない純関数だけを見る。

撮影と塗布は web 層のジョブが担う。
"""

import math

import pytest
from shapely import Polygon

from pcbasm.config import FlowCalibration
from pcbasm.geometry import Point2d
from pcbasm.pasting.paste_volume.estimator import PasteVolumePrediction
from pcbasm.pasting.paste_volume.runtime import (
    MAX_CORRECTION_SCALE,
    MIN_CORRECTION_SCALE,
    correct_rotations_per_ul,
    plan_flow_calibration,
)

OUTLINE = Polygon([(0.0, 0.0), (40.0, 0.0), (40.0, 30.0), (0.0, 30.0)])
ENABLED = FlowCalibration(calibration_file="cal.paste-volume.json")


def _accepted(volume_ul: float) -> PasteVolumePrediction:
    return PasteVolumePrediction(
        mean_volume_ul=volume_ul,
        std_volume_ul=volume_ul * 0.1,
        relative_std=0.1,
        accepted=True,
        rejection_reason=None,
    )


def _rejected(reason: str) -> PasteVolumePrediction:
    return PasteVolumePrediction(
        mean_volume_ul=0.0,
        std_volume_ul=0.0,
        relative_std=0.0,
        accepted=False,
        rejection_reason=reason,
    )


class TestPlanFlowCalibration:
    """測定点の配置."""

    def test_lays_out_the_configured_number_of_points_along_x(self):
        plan, error = plan_flow_calibration(
            config=ENABLED, point=Point2d(10.0, 12.0), outline=OUTLINE
        )

        assert error is None
        assert plan is not None
        assert plan.points == (
            Point2d(10.0, 12.0),
            Point2d(13.0, 12.0),
            Point2d(16.0, 12.0),
        )

    def test_carries_the_commanded_amount_and_crop_size(self):
        plan, error = plan_flow_calibration(
            config=ENABLED, point=Point2d(10.0, 12.0), outline=OUTLINE
        )

        assert error is None
        assert plan is not None
        assert plan.amount_ul == pytest.approx(0.2)
        assert plan.crop_size_mm == pytest.approx(2.0)
        assert plan.total_commanded_ul == pytest.approx(0.6)

    def test_is_disabled_when_no_point_is_configured(self):
        plan, error = plan_flow_calibration(config=ENABLED, point=None, outline=OUTLINE)

        assert error is None
        assert plan is None

    def test_is_disabled_when_the_point_count_is_zero(self):
        plan, error = plan_flow_calibration(
            config=FlowCalibration(
                calibration_file="cal.paste-volume.json", point_count=0
            ),
            point=Point2d(10.0, 12.0),
            outline=OUTLINE,
        )

        assert error is None
        assert plan is None

    def test_is_disabled_when_no_calibration_file_is_configured(self):
        plan, error = plan_flow_calibration(
            config=FlowCalibration(), point=Point2d(10.0, 12.0), outline=OUTLINE
        )

        assert error is None
        assert plan is None

    def test_rejects_a_starting_point_outside_the_board_outline(self):
        plan, error = plan_flow_calibration(
            config=ENABLED, point=Point2d(-1.0, 12.0), outline=OUTLINE
        )

        assert plan is None
        assert error is not None
        assert "基板外形" in error

    def test_rejects_a_row_that_runs_off_the_board(self):
        plan, error = plan_flow_calibration(
            config=ENABLED, point=Point2d(38.0, 12.0), outline=OUTLINE
        )

        assert plan is None
        assert error is not None
        assert "基板外形" in error

    def test_rejects_a_non_finite_point(self):
        plan, error = plan_flow_calibration(
            config=ENABLED, point=Point2d(math.nan, 12.0), outline=OUTLINE
        )

        assert plan is None
        assert error is not None

    def test_rejects_a_pitch_that_lets_the_next_dot_enter_the_crop(self):
        plan, error = plan_flow_calibration(
            config=FlowCalibration(
                calibration_file="cal.paste-volume.json",
                crop_size_mm=4.0,
                point_pitch_mm=3.0,
            ),
            point=Point2d(10.0, 12.0),
            outline=OUTLINE,
        )

        assert plan is None
        assert error is not None
        assert "crop" in error

    def test_allows_a_narrow_pitch_when_only_one_point_is_measured(self):
        plan, error = plan_flow_calibration(
            config=FlowCalibration(
                calibration_file="cal.paste-volume.json",
                crop_size_mm=4.0,
                point_pitch_mm=3.0,
                point_count=1,
            ),
            point=Point2d(10.0, 12.0),
            outline=OUTLINE,
        )

        assert error is None
        assert plan is not None
        assert plan.points == (Point2d(10.0, 12.0),)

    def test_a_single_point_plan_is_just_the_configured_point(self):
        plan, error = plan_flow_calibration(
            config=FlowCalibration(
                calibration_file="cal.paste-volume.json", point_count=1
            ),
            point=Point2d(10.0, 12.0),
            outline=OUTLINE,
        )

        assert error is None
        assert plan is not None
        assert plan.points == (Point2d(10.0, 12.0),)


class TestCorrectRotationsPerUl:
    """推定体積から ``rotations_per_ul`` を補正する."""

    def test_over_dispensing_raises_rotations_per_ul(self):
        outcome, error = correct_rotations_per_ul(
            [_accepted(0.25), _accepted(0.25), _accepted(0.25)],
            amount_ul=0.2,
            rotations_per_ul=1.0,
        )

        assert error is None
        assert outcome is not None
        assert outcome.ratio == pytest.approx(1.25)
        assert outcome.rotations_per_ul == pytest.approx(0.8)

    def test_under_dispensing_lowers_rotations_per_ul(self):
        outcome, error = correct_rotations_per_ul(
            [_accepted(0.16)], amount_ul=0.2, rotations_per_ul=1.0
        )

        assert error is None
        assert outcome is not None
        assert outcome.ratio == pytest.approx(0.8)
        assert outcome.rotations_per_ul == pytest.approx(1.25)

    def test_aggregates_by_total_volume_not_by_averaging_ratios(self):
        outcome, error = correct_rotations_per_ul(
            [_accepted(0.1), _accepted(0.3)], amount_ul=0.2, rotations_per_ul=2.0
        )

        assert error is None
        assert outcome is not None
        assert outcome.estimated_ul == pytest.approx(0.4)
        assert outcome.commanded_ul == pytest.approx(0.4)
        assert outcome.ratio == pytest.approx(1.0)
        assert outcome.rotations_per_ul == pytest.approx(2.0)

    def test_only_accepted_predictions_enter_the_aggregate(self):
        outcome, error = correct_rotations_per_ul(
            [
                _accepted(0.25),
                _rejected("diameter_below_reliable_range"),
                _accepted(0.25),
            ],
            amount_ul=0.2,
            rotations_per_ul=1.0,
        )

        assert error is None
        assert outcome is not None
        assert outcome.accepted_count == 2
        assert outcome.commanded_ul == pytest.approx(0.4)
        assert outcome.ratio == pytest.approx(1.25)
        assert outcome.rejections == ("diameter_below_reliable_range",)

    def test_no_accepted_prediction_means_no_correction(self):
        outcome, error = correct_rotations_per_ul(
            [_rejected("no_deposit_detected")], amount_ul=0.2, rotations_per_ul=1.0
        )

        assert outcome is None
        assert error is not None
        assert "no_deposit_detected" in error

    def test_an_empty_prediction_list_means_no_correction(self):
        outcome, error = correct_rotations_per_ul(
            [], amount_ul=0.2, rotations_per_ul=1.0
        )

        assert outcome is None
        assert error is not None

    def test_clamps_a_ratio_that_would_shrink_the_factor_too_far(self):
        outcome, error = correct_rotations_per_ul(
            [_accepted(2.0)], amount_ul=0.2, rotations_per_ul=1.0
        )

        assert error is None
        assert outcome is not None
        assert outcome.ratio == pytest.approx(10.0)
        assert outcome.clamped is True
        assert outcome.rotations_per_ul == pytest.approx(MIN_CORRECTION_SCALE)

    def test_clamps_a_ratio_that_would_grow_the_factor_too_far(self):
        outcome, error = correct_rotations_per_ul(
            [_accepted(0.01)], amount_ul=0.2, rotations_per_ul=1.0
        )

        assert error is None
        assert outcome is not None
        assert outcome.clamped is True
        assert outcome.rotations_per_ul == pytest.approx(MAX_CORRECTION_SCALE)

    def test_an_unclamped_correction_is_reported_as_such(self):
        outcome, error = correct_rotations_per_ul(
            [_accepted(0.22)], amount_ul=0.2, rotations_per_ul=1.0
        )

        assert error is None
        assert outcome is not None
        assert outcome.clamped is False

    def test_keeps_the_previous_factor_for_reporting(self):
        outcome, error = correct_rotations_per_ul(
            [_accepted(0.25)], amount_ul=0.2, rotations_per_ul=1.5
        )

        assert error is None
        assert outcome is not None
        assert outcome.previous_rotations_per_ul == pytest.approx(1.5)
        assert outcome.rotations_per_ul == pytest.approx(1.2)

    def test_a_zero_estimate_cannot_produce_a_factor(self):
        outcome, error = correct_rotations_per_ul(
            [_accepted(0.0)], amount_ul=0.2, rotations_per_ul=1.0
        )

        assert outcome is None
        assert error is not None

    @pytest.mark.parametrize("amount_ul", [0.0, -0.1, math.nan])
    def test_rejects_an_invalid_commanded_amount(self, amount_ul: float):
        outcome, error = correct_rotations_per_ul(
            [_accepted(0.2)], amount_ul=amount_ul, rotations_per_ul=1.0
        )

        assert outcome is None
        assert error is not None

    @pytest.mark.parametrize("rotations_per_ul", [0.0, -1.0, math.inf])
    def test_rejects_an_invalid_previous_factor(self, rotations_per_ul: float):
        outcome, error = correct_rotations_per_ul(
            [_accepted(0.2)], amount_ul=0.2, rotations_per_ul=rotations_per_ul
        )

        assert outcome is None
        assert error is not None
