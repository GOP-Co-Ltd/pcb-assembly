from __future__ import annotations

import math

import pytest

from pcbasm.pasting.paste_volume.calibration import (
    PasteVolumeCalibrationSample,
    calibrate_rotations_per_ul,
    failed_paste_volume_calibration,
)
from pcbasm.pasting.paste_volume.inference import PasteVolumePrediction


def _prediction(
    mean: float,
    *,
    accepted: bool = True,
    model_id: str = "model-a",
) -> PasteVolumePrediction:
    return PasteVolumePrediction(
        mean_volume_ul=mean,
        std_volume_ul=0.01,
        relative_std=0.1,
        accepted=accepted,
        rejection_reason=None if accepted else "low confidence",
        model_id=model_id,
    )


class TestCalibrateRotationsPerUl:
    def test_aggregates_accepted_samples_once(self):
        result = calibrate_rotations_per_ul(
            12.0,
            (
                PasteVolumeCalibrationSample(1.0, _prediction(0.8)),
                PasteVolumeCalibrationSample(2.0, _prediction(1.6)),
            ),
        )

        assert result.applied
        assert result.gain == pytest.approx(0.8)
        assert result.new_rotations_per_ul == pytest.approx(15.0)
        assert result.used_count == 2
        assert result.rejected_count == 0
        assert result.model_id == "model-a"
        assert not result.clamped

    def test_rejected_sample_is_excluded_from_both_totals(self):
        result = calibrate_rotations_per_ul(
            12.0,
            (
                PasteVolumeCalibrationSample(1.0, _prediction(1.0)),
                PasteVolumeCalibrationSample(100.0, _prediction(100.0, accepted=False)),
            ),
        )

        assert result.applied
        assert result.gain == pytest.approx(1.0)
        assert result.used_count == 1
        assert result.rejected_count == 1

    @pytest.mark.parametrize(
        ("estimated", "expected"),
        [(0.01, 30.0), (100.0, 10.0 / 3.0)],
    )
    def test_clamps_each_update_to_one_third_through_three_times(
        self, estimated, expected
    ):
        result = calibrate_rotations_per_ul(
            10.0,
            (PasteVolumeCalibrationSample(1.0, _prediction(estimated)),),
        )

        assert result.applied
        assert result.clamped
        assert result.new_rotations_per_ul == pytest.approx(expected)

    def test_zero_accepted_predictions_preserve_the_old_value(self):
        result = calibrate_rotations_per_ul(
            10.0,
            (PasteVolumeCalibrationSample(1.0, _prediction(1.0, accepted=False)),),
        )

        assert not result.applied
        assert result.new_rotations_per_ul == 10.0
        assert result.used_count == 0
        assert result.rejected_count == 1

    def test_mixed_model_predictions_abort_the_whole_update(self):
        result = calibrate_rotations_per_ul(
            10.0,
            (
                PasteVolumeCalibrationSample(1.0, _prediction(1.0)),
                PasteVolumeCalibrationSample(1.0, _prediction(1.0, model_id="model-b")),
            ),
        )

        assert not result.applied
        assert result.new_rotations_per_ul == 10.0
        assert result.used_count == 0
        assert result.rejected_count == 2
        assert "複数model" in str(result.rejection_reason)

    @pytest.mark.parametrize("invalid", [0.0, -1.0, math.nan, math.inf, True])
    def test_invalid_old_calibration_is_rejected(self, invalid):
        with pytest.raises(ValueError):
            calibrate_rotations_per_ul(invalid, ())


class TestFailedPasteVolumeCalibration:
    def test_represents_an_atomic_inference_failure_without_applying(self):
        result = failed_paste_volume_calibration(
            10.0,
            3,
            "camera moved",
            model_id="model-a",
        )

        assert not result.applied
        assert result.new_rotations_per_ul == 10.0
        assert result.rejected_count == 3
        assert result.rejection_reason == "camera moved"
