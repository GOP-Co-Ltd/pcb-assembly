"""次元別 slice と reliability bin による診断レポートの公開契約."""

from collections.abc import Sequence
from itertools import pairwise

import pytest
import torch

from ml.evaluation.regression import GaussianPredictions
from ml.evaluation.slices import (
    CategoricalDimension,
    DiagnosticReport,
    NumericDimension,
    SliceDimension,
    build_diagnostic_report,
)


def _predictions(
    mean: Sequence[float],
    target: Sequence[float],
    log_variance: Sequence[float] | None = None,
    sample_weight: Sequence[float] | None = None,
) -> GaussianPredictions:
    count = len(mean)
    return GaussianPredictions(
        mean=torch.tensor(mean, dtype=torch.float32),
        log_variance=torch.tensor(
            [0.0] * count if log_variance is None else log_variance,
            dtype=torch.float32,
        ),
        target=torch.tensor(target, dtype=torch.float32),
        sample_weight=torch.tensor(
            [1.0] * count if sample_weight is None else sample_weight,
            dtype=torch.float32,
        ),
    )


def _report(
    predictions: GaussianPredictions,
    dimensions: Sequence[SliceDimension] = (),
    reliability_bin_count: int = 5,
) -> DiagnosticReport:
    report, reason = build_diagnostic_report(
        predictions,
        dimensions=dimensions,
        reliability_bin_count=reliability_bin_count,
    )

    assert reason is None
    assert report is not None
    return report


def _twelve_predictions(
    log_variance: Sequence[float] | None = None,
) -> GaussianPredictions:
    mean = [1.0 + 0.25 * index for index in range(12)]
    target = [2.0 + 0.2 * index for index in range(12)]
    return _predictions(mean, target, log_variance=log_variance)


class TestCategoricalDimension:
    """カテゴリ次元の値列は予測と同じ並び・同じ件数."""

    def test_accepts_an_aligned_value_list(self):
        dimension = CategoricalDimension(name="machine", values=("a", "b"))

        assert dimension.validate(2) is None

    def test_rejects_a_length_mismatch(self):
        dimension = CategoricalDimension(name="machine", values=("a", "b"))

        error = dimension.validate(3)

        assert error is not None
        assert "machine" in error

    def test_rejects_an_empty_name(self):
        error = CategoricalDimension(name="", values=("a",)).validate(1)

        assert error == "次元名が空です"


class TestNumericDimension:
    """数値次元は bucket 数と値の有限性まで検証する."""

    def test_accepts_an_aligned_value_list(self):
        dimension = NumericDimension(name="area", values=(1.0, 2.0))

        assert dimension.validate(2) is None

    @pytest.mark.parametrize(
        ("dimension", "expected"),
        [
            (NumericDimension(name="area", values=(1.0,), bucket_count=0), "bucket"),
            (
                NumericDimension(name="area", values=(float("nan"),)),
                "非有限値",
            ),
        ],
    )
    def test_rejects_an_unusable_definition(
        self, dimension: NumericDimension, expected: str
    ):
        error = dimension.validate(1)

        assert error is not None
        assert expected in error


class TestBuildDiagnosticReport:
    """全体 metric、次元別 slice、reliability bin をまとめる."""

    def test_reports_the_overall_metrics(self):
        report = _report(_predictions([1.0, 2.0, 4.0], [2.0, 2.0, 2.0]))

        assert report.overall.valid_sample_count == 3
        assert report.overall.mean_absolute_error == pytest.approx(1.0)
        assert report.slices == ()

    def test_places_every_sample_in_exactly_one_numeric_bucket(self):
        predictions = _twelve_predictions()
        dimension = NumericDimension(
            name="area", values=tuple(float(index) for index in range(12))
        )

        report = _report(predictions, [dimension])

        assert len(report.slices) == 4
        assert sum(entry.sample_count for entry in report.slices) == 12
        assert all(entry.dimension == "area" for entry in report.slices)
        assert report.slices[-1].value.endswith("]")
        assert all(entry.value.endswith(")") for entry in report.slices[:-1])

    def test_orders_categorical_values_ascending(self):
        predictions = _predictions([1.0, 2.0, 4.0, 3.0], [2.0] * 4)
        dimension = CategoricalDimension(
            name="machine", values=("beta", "alpha", "beta", "alpha")
        )

        report = _report(predictions, [dimension])

        assert [entry.value for entry in report.slices] == ["alpha", "beta"]
        assert [entry.sample_count for entry in report.slices] == [2, 2]

    def test_keeps_dimensions_in_the_given_order(self):
        predictions = _predictions([1.0, 2.0, 4.0, 3.0], [2.0] * 4)
        numeric = NumericDimension(
            name="area", values=(1.0, 2.0, 3.0, 4.0), bucket_count=2
        )
        categorical = CategoricalDimension(
            name="machine", values=("beta", "alpha", "beta", "alpha")
        )

        report = _report(predictions, [numeric, categorical])

        assert [entry.dimension for entry in report.slices] == [
            "area",
            "area",
            "machine",
            "machine",
        ]

    def test_collapses_a_constant_numeric_dimension_into_one_bucket(self):
        predictions = _predictions([1.0, 2.0, 4.0, 3.0], [2.0] * 4)
        dimension = NumericDimension(name="area", values=(5.0,) * 4)

        report = _report(predictions, [dimension])

        assert len(report.slices) == 1
        assert report.slices[0].value == "[5,5]"
        assert report.slices[0].sample_count == 4

    def test_drops_numeric_buckets_without_samples(self):
        predictions = _predictions([1.0, 4.0], [2.0, 2.0])
        dimension = NumericDimension(name="area", values=(5.0, 0.0), bucket_count=5)

        report = _report(predictions, [dimension])

        assert [entry.value for entry in report.slices] == ["[0,1)", "[4,5]"]
        assert all(entry.sample_count > 0 for entry in report.slices)
        assert all(entry.metrics is not None for entry in report.slices)

    def test_keeps_bucket_labels_unique_within_a_dimension(self):
        predictions = _predictions([1.0, 2.0, 4.0, 3.0], [2.0] * 4)
        dimension = NumericDimension(
            name="area",
            values=(1.000001, 1.000002, 1.000003, 1.000004),
            bucket_count=4,
        )

        report = _report(predictions, [dimension])

        labels = [entry.value for entry in report.slices]
        assert len(set(labels)) == len(labels)
        assert all(label.startswith("[1.000") for label in labels)

    def test_identifies_the_slice_by_its_structured_fields_not_the_reason(self):
        predictions = _predictions([1.0, 2.0, float("nan"), float("nan")], [2.0] * 4)
        dimension = CategoricalDimension(
            name="machine", values=("alpha", "alpha", "beta", "beta")
        )

        report = _report(predictions, [dimension])

        broken = report.slices[1]
        assert broken.dimension == "machine"
        assert broken.value == "beta"
        assert broken.reason is not None
        assert "machine" not in broken.reason
        assert "beta" not in broken.reason

    def test_reports_a_reason_for_a_slice_without_valid_samples(self):
        predictions = _predictions([1.0, 2.0, float("nan"), float("nan")], [2.0] * 4)
        dimension = CategoricalDimension(
            name="machine", values=("alpha", "alpha", "beta", "beta")
        )

        report = _report(predictions, [dimension])

        broken = report.slices[1]
        assert broken.value == "beta"
        assert broken.sample_count == 2
        assert broken.metrics is None
        assert broken.reason is not None
        assert report.slices[0].metrics is not None
        assert report.slices[0].reason is None

    def test_rejects_a_dimension_of_the_wrong_length(self):
        predictions = _predictions([1.0, 2.0], [2.0, 2.0])
        dimension = CategoricalDimension(name="machine", values=("a",))

        with pytest.raises(ValueError, match="machine"):
            build_diagnostic_report(
                predictions, dimensions=[dimension], reliability_bin_count=2
            )

    def test_reports_a_reason_instead_of_a_report_when_nothing_is_valid(self):
        report, reason = build_diagnostic_report(
            _predictions([float("nan"), -1.0], [2.0, 2.0]), dimensions=()
        )

        assert report is None
        assert reason is not None
        assert "有効な sample がありません" in reason


class TestReliabilityBins:
    """予測 standard deviation の帯ごとの校正診断."""

    def test_bins_are_monotonic_and_do_not_overlap(self):
        report = _report(
            _twelve_predictions(
                log_variance=[-2.0 + 0.4 * index for index in range(12)]
            )
        )

        bins = report.reliability_bins
        assert len(bins) == 5
        assert sum(entry.sample_count for entry in bins) == 12
        for entry in bins:
            assert entry.lower_standard_deviation <= entry.upper_standard_deviation
        for lower, upper in pairwise(bins):
            assert lower.upper_standard_deviation < upper.lower_standard_deviation

    def test_does_not_split_equal_standard_deviations(self):
        report = _report(_twelve_predictions(log_variance=[0.0, 1.0, 2.0] * 4))

        bins = report.reliability_bins
        assert len(bins) == 3
        assert [entry.sample_count for entry in bins] == [4, 4, 4]
        for entry in bins:
            assert entry.lower_standard_deviation == pytest.approx(
                entry.upper_standard_deviation
            )

    def test_counts_only_valid_samples(self):
        predictions = _predictions(
            [1.0, 2.0, 4.0, float("nan")],
            [2.0, 2.0, 2.0, 2.0],
            log_variance=[-1.0, 0.0, 1.0, 2.0],
        )

        report = _report(predictions, reliability_bin_count=3)

        assert report.overall.valid_sample_count == 3
        assert sum(entry.sample_count for entry in report.reliability_bins) == 3

    def test_summarizes_each_bin_with_weighted_statistics(self):
        predictions = _predictions(
            [1.0, 2.0, 4.0], [2.0, 2.0, 2.0], sample_weight=[3.0, 1.0, 1.0]
        )

        single = _report(predictions, reliability_bin_count=1).reliability_bins[0]

        # 予測 std は 3 件とも 1、絶対誤差は [1, 0, 2]、weight は [3, 1, 1]
        assert single.sample_count == 3
        assert single.mean_predicted_standard_deviation == pytest.approx(1.0)
        assert single.observed_root_mean_squared_error == pytest.approx(
            ((3.0 * 1.0 + 1.0 * 0.0 + 1.0 * 4.0) / 5.0) ** 0.5
        )
        assert single.one_standard_deviation_coverage == pytest.approx(4.0 / 5.0)

    def test_uses_the_same_definitions_as_the_overall_and_slice_metrics(self):
        predictions = _predictions(
            [1.0, 2.0, 4.0],
            [2.0, 2.0, 2.0],
            log_variance=[0.0, 2.0, -2.0],
            sample_weight=[3.0, 1.0, 1.0],
        )
        dimension = CategoricalDimension(name="machine", values=("a", "a", "a"))

        report = _report(predictions, [dimension], reliability_bin_count=1)

        single = report.reliability_bins[0]
        whole = report.slices[0].metrics
        assert whole is not None
        assert single.sample_count == 3
        assert single.mean_predicted_standard_deviation == pytest.approx(
            report.overall.mean_predicted_standard_deviation
        )
        assert single.mean_predicted_standard_deviation == pytest.approx(
            whole.mean_predicted_standard_deviation
        )
        assert single.observed_root_mean_squared_error == pytest.approx(
            report.overall.root_mean_squared_error
        )
        assert single.one_standard_deviation_coverage == pytest.approx(
            report.overall.one_standard_deviation_coverage
        )

    def test_excludes_zero_weight_samples_from_the_bins(self):
        predictions = _predictions(
            [1.0, 2.0, 4.0, 100.0],
            [2.0] * 4,
            log_variance=[-1.0, 0.0, 1.0, 3.0],
            sample_weight=[1.0, 1.0, 1.0, 0.0],
        )

        report = _report(predictions, reliability_bin_count=2)

        assert report.overall.weight_sum == pytest.approx(3.0)
        assert sum(entry.sample_count for entry in report.reliability_bins) == 3
        assert all(
            entry.observed_root_mean_squared_error < 3.0
            for entry in report.reliability_bins
        )

    @pytest.mark.parametrize("reliability_bin_count", [0, -1])
    def test_returns_no_bins_below_one_bin(self, reliability_bin_count: int):
        report = _report(
            _twelve_predictions(), reliability_bin_count=reliability_bin_count
        )

        assert report.reliability_bins == ()
        assert report.overall.valid_sample_count == 12
