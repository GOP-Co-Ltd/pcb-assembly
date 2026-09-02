from __future__ import annotations

from pathlib import Path

import attrs
import pytest

from pcbasm.pasting.paste_volume.data import build_sample_index, resolve_dataset_inputs
from pcbasm.pasting.paste_volume.reporting import (
    Prediction,
    build_cross_group_plan,
    build_cross_group_plans,
    build_diagnostic_report,
    calculate_slice_metrics,
)
from tests.pcbasm.pasting.paste_volume.support_data import write_synthetic_session


class TestCrossGroupPlanning:
    def test_builds_true_machine_held_out_folds(self, tmp_path: Path):
        for index, machine in enumerate(("a", "a", "b", "b")):
            write_synthetic_session(
                tmp_path,
                f"s{index}",
                machine_id=machine,
                paste_lot=f"lot-{index % 2}",
                nozzle_diameter_mm=0.3 + (index % 2) * 0.1,
                content_seed=index,
            )
        composite = resolve_dataset_inputs(roots=(tmp_path,))
        samples = build_sample_index(composite)

        plan = build_cross_group_plan(
            samples, composite.composite_fingerprint, "machine", seed=8
        )

        assert plan.available
        assert len(plan.folds) == 2
        by_id = {sample.sample_id: sample for sample in samples}
        for fold in plan.folds:
            test_groups = {
                by_id[sample_id].machine_id for sample_id in fold.split.test_sample_ids
            }
            train_groups = {
                by_id[sample_id].machine_id for sample_id in fold.split.train_sample_ids
            }
            validation_groups = {
                by_id[sample_id].machine_id
                for sample_id in fold.split.validation_sample_ids
            }
            assert test_groups == {fold.held_out_group}
            assert fold.held_out_group not in train_groups | validation_groups
            assert set(fold.train_session_ids).isdisjoint(fold.held_out_session_ids)

    def test_builds_machine_lot_and_nozzle_plans(self, tmp_path: Path):
        for index in range(4):
            write_synthetic_session(
                tmp_path,
                f"s{index}",
                machine_id=f"m-{index % 2}",
                paste_lot=f"lot-{index % 2}",
                nozzle_diameter_mm=0.3 + (index % 2) * 0.1,
                content_seed=index,
            )
        composite = resolve_dataset_inputs(roots=(tmp_path,))

        plans = build_cross_group_plans(
            build_sample_index(composite), composite.composite_fingerprint
        )

        assert [plan.dimension for plan in plans] == ["machine", "paste_lot", "nozzle"]
        assert all(plan.available and len(plan.folds) == 2 for plan in plans)

    def test_marks_single_group_and_too_few_training_sessions_unavailable(
        self, tmp_path: Path
    ):
        write_synthetic_session(tmp_path, "only", machine_id="same")
        composite = resolve_dataset_inputs(roots=(tmp_path,))
        one_group = build_cross_group_plan(
            build_sample_index(composite), composite.composite_fingerprint, "machine"
        )
        assert not one_group.available
        assert "2種類未満" in str(one_group.reason)
        assert one_group.folds == ()

        write_synthetic_session(tmp_path, "other", machine_id="other", content_seed=2)
        composite = resolve_dataset_inputs(roots=(tmp_path,))
        too_few = build_cross_group_plan(
            build_sample_index(composite), composite.composite_fingerprint, "machine"
        )
        assert not too_few.available
        assert "2個未満" in str(too_few.reason)

    def test_rejects_mixed_group_within_session(self, tmp_path: Path):
        write_synthetic_session(tmp_path, "session", pad_count=2)
        composite = resolve_dataset_inputs(roots=(tmp_path,))
        samples = list(build_sample_index(composite))
        samples[1] = attrs.evolve(samples[1], machine_id="different")

        with pytest.raises(ValueError, match="混在"):
            build_cross_group_plan(samples, composite.composite_fingerprint, "machine")


class TestDiagnosticReporting:
    def test_calculates_physical_metrics_and_invalid_count(self, tmp_path: Path):
        write_synthetic_session(tmp_path, "session", pad_count=3)
        samples = build_sample_index(resolve_dataset_inputs(roots=(tmp_path,)))
        predictions = {
            samples[0].sample_id: Prediction(
                samples[0].sample_id, samples[0].measured_volume_ul + 0.01, 0.02
            ),
            samples[1].sample_id: Prediction(
                samples[1].sample_id, samples[1].measured_volume_ul - 0.02, 0.03
            ),
            samples[2].sample_id: Prediction(samples[2].sample_id, float("nan"), 0.01),
        }

        metrics = calculate_slice_metrics(samples, predictions)

        assert metrics.sample_count == 3
        assert metrics.valid_prediction_count == 2
        assert metrics.invalid_prediction_count == 1
        assert metrics.mae_ul == pytest.approx(0.015)
        assert metrics.rmse_ul is not None and metrics.rmse_ul > 0
        assert metrics.gaussian_nll is not None
        assert metrics.one_std_coverage == pytest.approx(1.0)

    def test_reports_required_diagnostic_slices_and_reliability(self, tmp_path: Path):
        for index in range(4):
            write_synthetic_session(
                tmp_path,
                f"s{index}",
                machine_id=f"m-{index % 2}",
                paste_lot=f"lot-{index % 2}",
                nozzle_diameter_mm=0.3 + 0.1 * (index % 2),
                width=48 + index * 8,
                height=40 + index * 4,
                pixel_per_mm=15.0 + index,
                pad_count=3,
                content_seed=index,
            )
        samples = build_sample_index(resolve_dataset_inputs(roots=(tmp_path,)))
        predictions = tuple(
            Prediction(
                sample.sample_id,
                sample.measured_volume_ul + (index % 3 - 1) * 0.005,
                0.01 + index * 0.001,
            )
            for index, sample in enumerate(samples)
        )

        report = build_diagnostic_report(samples, predictions)

        dimensions = {item.dimension for item in report.slices}
        assert {
            "target_volume_ul",
            "image_area_pixels",
            "aspect_ratio",
            "pixel_per_mm",
            "dispense_mode",
            "machine",
            "paste_lot",
            "nozzle",
            "source",
        } <= dimensions
        assert report.overall.sample_count == len(samples)
        assert report.reliability_bins
        assert sum(item.sample_count for item in report.reliability_bins) == len(
            samples
        )

    def test_counts_deduplicated_sample_only_under_primary_source(self, tmp_path: Path):
        write_synthetic_session(tmp_path, "session")
        samples = tuple(
            attrs.evolve(sample, source_ids=("source-z", "source-a"))
            for sample in build_sample_index(resolve_dataset_inputs(roots=(tmp_path,)))
        )
        predictions = tuple(
            Prediction(sample.sample_id, sample.measured_volume_ul, 0.01)
            for sample in samples
        )

        report = build_diagnostic_report(samples, predictions)
        source_slices = [item for item in report.slices if item.dimension == "source"]

        assert [(item.value, item.metrics.sample_count) for item in source_slices] == [
            ("source-a", len(samples))
        ]

    def test_rejects_duplicate_and_unknown_prediction_ids(self, tmp_path: Path):
        write_synthetic_session(tmp_path, "session")
        samples = build_sample_index(resolve_dataset_inputs(roots=(tmp_path,)))
        duplicate = Prediction(samples[0].sample_id, 0.1, 0.01)
        with pytest.raises(ValueError, match="重複"):
            build_diagnostic_report(samples, (duplicate, duplicate))
        with pytest.raises(ValueError, match="未知sample_id"):
            build_diagnostic_report(samples, (Prediction("unknown", 0.1, 0.01),))
