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
    overlapping_crops,
    plan_flow_calibration,
    validate_crop_separation,
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
    """基板ごとに 1 点ずつ与えた測定位置の検証."""

    def test_keeps_the_configured_points_in_order(self):
        points = (Point2d(10.0, 12.0), Point2d(13.0, 12.0), Point2d(16.0, 20.0))

        plan, error = plan_flow_calibration(
            config=ENABLED, points=points, outline=OUTLINE
        )

        assert error is None
        assert plan is not None
        assert plan.points == points

    def test_carries_the_commanded_amount_and_crop_size(self):
        plan, error = plan_flow_calibration(
            config=ENABLED,
            points=(Point2d(10.0, 12.0), Point2d(13.0, 12.0), Point2d(16.0, 12.0)),
            outline=OUTLINE,
        )

        assert error is None
        assert plan is not None
        assert plan.amount_ul == pytest.approx(0.2)
        assert plan.crop_size_mm == pytest.approx(2.0)
        assert plan.settle_seconds == pytest.approx(10.0)
        assert plan.total_commanded_ul == pytest.approx(0.6)

    def test_is_disabled_when_no_point_is_configured(self):
        plan, error = plan_flow_calibration(config=ENABLED, points=(), outline=OUTLINE)

        assert error is None
        assert plan is None

    def test_is_disabled_when_no_calibration_file_is_configured(self):
        plan, error = plan_flow_calibration(
            config=FlowCalibration(),
            points=(Point2d(10.0, 12.0),),
            outline=OUTLINE,
        )

        assert error is None
        assert plan is None

    def test_rejects_a_point_outside_the_board_outline(self):
        plan, error = plan_flow_calibration(
            config=ENABLED,
            points=(Point2d(10.0, 12.0), Point2d(-1.0, 12.0)),
            outline=OUTLINE,
        )

        assert plan is None
        assert error is not None
        assert "基板外形" in error

    def test_rejects_a_non_finite_point(self):
        plan, error = plan_flow_calibration(
            config=ENABLED, points=(Point2d(math.nan, 12.0),), outline=OUTLINE
        )

        assert plan is None
        assert error is not None

    def test_rejects_points_whose_crops_overlap(self):
        """隣のドットが crop へ写り込むと最大連結成分が別のドットになる."""
        plan, error = plan_flow_calibration(
            config=FlowCalibration(
                calibration_file="cal.paste-volume.json", crop_size_mm=4.0
            ),
            points=(Point2d(10.0, 12.0), Point2d(13.0, 12.0)),
            outline=OUTLINE,
        )

        assert plan is None
        assert error is not None
        assert "撮影範囲" in error

    def test_allows_points_whose_crops_only_touch(self):
        plan, error = plan_flow_calibration(
            config=FlowCalibration(
                calibration_file="cal.paste-volume.json", crop_size_mm=4.0
            ),
            points=(Point2d(10.0, 12.0), Point2d(14.0, 12.0)),
            outline=OUTLINE,
        )

        assert error is None
        assert plan is not None

    def test_separation_on_one_axis_is_enough(self):
        """Crop は正方形なので、X が近くても Y が離れていれば重ならない."""
        plan, error = plan_flow_calibration(
            config=FlowCalibration(
                calibration_file="cal.paste-volume.json", crop_size_mm=2.0
            ),
            points=(Point2d(10.0, 12.0), Point2d(10.0, 15.0)),
            outline=OUTLINE,
        )

        assert error is None
        assert plan is not None

    def test_a_single_point_is_always_separated_enough(self):
        plan, error = plan_flow_calibration(
            config=FlowCalibration(
                calibration_file="cal.paste-volume.json", crop_size_mm=8.0
            ),
            points=(Point2d(10.0, 12.0),),
            outline=OUTLINE,
        )

        assert error is None
        assert plan is not None
        assert plan.points == (Point2d(10.0, 12.0),)


class TestValidateCropSeparation:
    """撮影範囲の重なり判定（保存では撥ねず、計画時にだけ効く）."""

    def test_reports_which_pair_overlaps_with_1_based_numbers(self):
        error = validate_crop_separation(
            points=(Point2d(0.0, 0.0), Point2d(10.0, 0.0), Point2d(11.0, 0.0)),
            crop_size_mm=2.0,
        )

        assert error is not None
        assert "測定位置 2 と 3" in error

    def test_accepts_an_empty_set(self):
        assert validate_crop_separation(points=(), crop_size_mm=2.0) is None

    def test_rejects_a_non_positive_crop_size(self):
        error = validate_crop_separation(points=(Point2d(0.0, 0.0),), crop_size_mm=0.0)

        assert error is not None


class TestOverlappingCrops:
    """図で置き直しの対象を示すための、点ごとの重なりフラグ."""

    def test_marks_both_points_of_an_overlapping_pair(self):
        flags = overlapping_crops(
            (Point2d(0.0, 0.0), Point2d(1.0, 0.0), Point2d(10.0, 0.0)),
            crop_size_mm=2.0,
        )

        assert flags == (True, True, False)

    def test_marks_nothing_when_all_are_separated(self):
        flags = overlapping_crops(
            (Point2d(0.0, 0.0), Point2d(3.0, 0.0)), crop_size_mm=2.0
        )

        assert flags == (False, False)

    def test_agrees_with_the_validation_used_for_planning(self):
        points = (Point2d(0.0, 0.0), Point2d(1.5, 0.0))

        overlapping = any(overlapping_crops(points, crop_size_mm=2.0))
        rejected = validate_crop_separation(points=points, crop_size_mm=2.0)

        assert overlapping is (rejected is not None)

    def test_a_non_positive_crop_size_marks_nothing(self):
        assert overlapping_crops((Point2d(0.0, 0.0),), crop_size_mm=0.0) == (False,)


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
