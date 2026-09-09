"""Gaussian 回帰 metric と log 分散 offset の公開契約."""

import math
from collections.abc import Sequence

import attrs
import pytest
import torch
from torch import Tensor

from ml.evaluation.regression import (
    GaussianPredictions,
    GaussianRegressionMetrics,
    MeanSaturationDiagnostic,
    ZeroTargetMetrics,
)

TOLERANCE = 1e-12


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


def _metrics(predictions: GaussianPredictions) -> GaussianRegressionMetrics:
    metrics, reason = GaussianRegressionMetrics.measure(predictions)

    assert reason is None
    assert metrics is not None
    return metrics


def _absolute_relative_error(mean: Tensor, target: Tensor) -> Tensor:
    return ((mean - target) / target).abs()


class TestGaussianPredictions:
    """評価対象の tensor を 1 次元・float64・CPU へそろえる."""

    def test_normalizes_shape_dtype_and_gradient(self):
        mean = torch.full((2, 1), 2.0, requires_grad=True)

        predictions = GaussianPredictions(
            mean=mean,
            log_variance=torch.zeros(2, 1),
            target=torch.full((2, 1), 2.0),
            sample_weight=torch.ones(2, 1),
        )

        assert predictions.mean.shape == (2,)
        assert predictions.mean.dtype == torch.float64
        assert predictions.mean.device.type == "cpu"
        assert not predictions.mean.requires_grad

    def test_accepts_an_aligned_set(self):
        assert _predictions([1.0, 2.0], [1.0, 2.0]).validate() is None

    def test_rejects_a_length_mismatch(self):
        predictions = GaussianPredictions(
            mean=torch.ones(3),
            log_variance=torch.zeros(3),
            target=torch.ones(2),
            sample_weight=torch.ones(3),
        )

        error = predictions.validate()

        assert error is not None
        assert "件数" in error

    def test_rejects_an_empty_set(self):
        predictions = GaussianPredictions(
            mean=torch.zeros(0),
            log_variance=torch.zeros(0),
            target=torch.zeros(0),
            sample_weight=torch.zeros(0),
        )

        error = predictions.validate()

        assert error is not None
        assert "1 件も" in error

    def test_compares_by_identity(self):
        predictions = _predictions([1.0, 2.0], [2.0, 2.0])
        other = _predictions([9.0, 9.0], [1.0, 1.0])

        assert predictions == predictions
        assert predictions != other
        assert predictions != predictions.with_log_variance_offset(2.0)
        assert len({predictions, other}) == 2

    def test_offsets_only_the_log_variance(self):
        predictions = _predictions([1.0, 2.0], [1.0, 2.0], log_variance=[-1.0, 0.5])

        shifted = predictions.with_log_variance_offset(2.0)

        assert torch.allclose(
            shifted.log_variance, torch.tensor([1.0, 2.5], dtype=torch.float64)
        )
        assert torch.equal(shifted.mean, predictions.mean)
        assert torch.equal(shifted.target, predictions.target)


class TestGaussianRegressionMetricsMeasure:
    """重み付き集計と、無効 sample の除外."""

    def test_matches_hand_computed_values(self):
        metrics = _metrics(_predictions([1.0, 2.0, 4.0], [2.0, 2.0, 2.0]))

        assert metrics.sample_count == 3
        assert metrics.valid_sample_count == 3
        assert metrics.invalid_sample_count == 0
        assert metrics.weight_sum == pytest.approx(3.0)
        assert metrics.negative_log_likelihood == pytest.approx(5.0 / 6.0)
        assert metrics.mean_absolute_error == pytest.approx(1.0)
        assert metrics.root_mean_squared_error == pytest.approx((5.0 / 3.0) ** 0.5)
        assert metrics.relative_error_mean == pytest.approx(1.0 / 6.0)
        assert metrics.relative_error_standard_deviation == pytest.approx(0.6236095645)
        assert metrics.relative_error_score == pytest.approx(0.7902762312)
        assert metrics.median_absolute_relative_error == pytest.approx(0.5)
        assert metrics.p95_absolute_relative_error == pytest.approx(0.95)
        assert metrics.one_standard_deviation_coverage == pytest.approx(2.0 / 3.0)
        assert metrics.mean_predicted_standard_deviation == pytest.approx(1.0)

    def test_weights_change_the_aggregate(self):
        uniform = _metrics(_predictions([1.0, 2.0, 4.0], [2.0, 2.0, 2.0]))

        weighted = _metrics(
            _predictions(
                [1.0, 2.0, 4.0], [2.0, 2.0, 2.0], sample_weight=[3.0, 1.0, 1.0]
            )
        )

        assert weighted.weight_sum == pytest.approx(5.0)
        assert weighted.relative_error_mean == pytest.approx(
            (3.0 * -0.5 + 0.0 + 1.0) / 5.0
        )
        assert weighted.relative_error_mean != pytest.approx(
            uniform.relative_error_mean
        )
        assert weighted.negative_log_likelihood != pytest.approx(
            uniform.negative_log_likelihood
        )

    @pytest.mark.parametrize("fraction", [0.5, 0.95])
    def test_uniform_weights_match_torch_quantile(self, fraction: float):
        mean = [1.0, 2.5, 3.0, 0.5, 4.0, 2.0, 6.0]
        target = [2.0, 2.0, 4.0, 1.0, 2.0, 3.0, 5.0]
        expected = torch.quantile(
            _absolute_relative_error(
                torch.tensor(mean, dtype=torch.float64),
                torch.tensor(target, dtype=torch.float64),
            ),
            fraction,
            interpolation="linear",
        )

        metrics = _metrics(_predictions(mean, target))

        actual = (
            metrics.median_absolute_relative_error
            if fraction == 0.5
            else metrics.p95_absolute_relative_error
        )
        assert actual == pytest.approx(float(expected.item()), abs=TOLERANCE)

    def test_fixes_the_percentile_of_non_uniform_weights(self):
        # 絶対相対誤差 [0.5, 0, 1, 3]、weight [1, 3, 1, 1]。
        # 昇順の位置 (C_i - w_i) / (W - w_i) は [0, 0.6, 0.8, 1.0]
        metrics = _metrics(
            _predictions(
                [3.0, 2.0, 4.0, 8.0],
                [2.0, 2.0, 2.0, 2.0],
                sample_weight=[1.0, 3.0, 1.0, 1.0],
            )
        )

        assert metrics.median_absolute_relative_error == pytest.approx(
            0.0 + (0.5 - 0.0) / (0.6 - 0.0) * (0.5 - 0.0)
        )
        assert metrics.p95_absolute_relative_error == pytest.approx(
            1.0 + (0.95 - 0.8) / (1.0 - 0.8) * (3.0 - 1.0)
        )

    def test_does_not_match_replicating_samples_by_their_weight(self):
        weighted = _metrics(
            _predictions(
                [3.0, 2.0, 4.0, 8.0],
                [2.0, 2.0, 2.0, 2.0],
                sample_weight=[1.0, 3.0, 1.0, 1.0],
            )
        )

        replicated = _metrics(_predictions([3.0, 2.0, 2.0, 2.0, 4.0, 8.0], [2.0] * 6))

        assert replicated.median_absolute_relative_error == pytest.approx(0.25)
        assert weighted.median_absolute_relative_error != pytest.approx(
            replicated.median_absolute_relative_error
        )

    def test_handles_a_single_sample(self):
        metrics = _metrics(_predictions([3.0], [2.0]))

        assert metrics.valid_sample_count == 1
        assert metrics.median_absolute_relative_error == pytest.approx(0.5)
        assert metrics.p95_absolute_relative_error == pytest.approx(0.5)

    @pytest.mark.parametrize(
        ("mean", "target", "log_variance", "sample_weight"),
        [
            ([1.0, float("nan")], [2.0, 2.0], None, None),
            ([1.0, -1.0], [2.0, 2.0], None, None),
            ([1.0, 2.0], [2.0, 0.0], None, None),
            ([1.0, 2.0], [2.0, 2.0], [0.0, float("inf")], None),
            ([1.0, 2.0], [2.0, 2.0], None, [1.0, -1.0]),
        ],
    )
    def test_excludes_an_invalid_sample_instead_of_raising(
        self,
        mean: list[float],
        target: list[float],
        log_variance: list[float] | None,
        sample_weight: list[float] | None,
    ):
        metrics = _metrics(_predictions(mean, target, log_variance, sample_weight))

        assert metrics.sample_count == 2
        assert metrics.valid_sample_count == 1
        assert metrics.invalid_sample_count == 1
        assert metrics.mean_absolute_error == pytest.approx(1.0)

    def test_matches_the_metrics_of_the_valid_subset(self):
        expected = _metrics(_predictions([1.0, 2.0, 4.0], [2.0, 2.0, 2.0]))

        metrics = _metrics(
            _predictions([1.0, 2.0, 4.0, float("nan")], [2.0, 2.0, 2.0, 2.0])
        )

        assert metrics.invalid_sample_count == 1
        assert metrics.negative_log_likelihood == pytest.approx(
            expected.negative_log_likelihood
        )
        assert metrics.p95_absolute_relative_error == pytest.approx(
            expected.p95_absolute_relative_error
        )

    def test_rejects_a_fully_invalid_set(self):
        metrics, reason = GaussianRegressionMetrics.measure(
            _predictions([float("nan"), -1.0], [2.0, 2.0])
        )

        assert metrics is None
        assert reason is not None
        assert "有効な sample がありません" in reason

    def test_rejects_a_zero_total_weight(self):
        metrics, reason = GaussianRegressionMetrics.measure(
            _predictions([1.0, 2.0], [2.0, 2.0], sample_weight=[0.0, 0.0])
        )

        assert metrics is None
        assert reason is not None
        assert "weight 合計が 0" in reason

    def test_rejects_an_empty_set(self):
        metrics, reason = GaussianRegressionMetrics.measure(_predictions([], []))

        assert metrics is None
        assert reason == "予測が 1 件もありません"

    def test_stays_finite_at_the_log_variance_clamp_limits(self):
        metrics = _metrics(
            _predictions([1.0, 4.0], [2.0, 2.0], log_variance=[-14.0, 5.0])
        )

        assert math.isfinite(metrics.negative_log_likelihood)
        assert math.isfinite(metrics.mean_predicted_standard_deviation)
        assert metrics.one_standard_deviation_coverage == pytest.approx(0.5)


class TestGaussianPredictionsFitLogVarianceOffset:
    """負の対数尤度を最小にする scalar offset."""

    def test_recovers_a_known_offset(self):
        predictions = _predictions([3.0, 1.0], [2.0, 2.0], log_variance=[-2.0, -2.0])

        offset, reason = predictions.fit_log_variance_offset()

        assert reason is None
        assert offset == pytest.approx(2.0)

    def test_beats_its_neighbours_in_negative_log_likelihood(self):
        predictions = _predictions(
            [3.0, 1.5, 2.4, 1.0], [2.0, 2.0, 2.0, 2.0], log_variance=[-3.0] * 4
        )
        offset, reason = predictions.fit_log_variance_offset()

        assert reason is None
        assert offset is not None
        fitted = _metrics(predictions.with_log_variance_offset(offset))
        for delta in (-0.1, 0.1):
            neighbour = _metrics(predictions.with_log_variance_offset(offset + delta))

            assert fitted.negative_log_likelihood < neighbour.negative_log_likelihood

    def test_rejects_a_perfect_fit(self):
        offset, reason = _predictions([2.0, 3.0], [2.0, 3.0]).fit_log_variance_offset()

        assert offset is None
        assert reason is not None
        assert "log 分散 offset" in reason

    def test_rejects_a_fully_invalid_set(self):
        offset, reason = _predictions([float("nan")], [2.0]).fit_log_variance_offset()

        assert offset is None
        assert reason is not None
        assert "有効な sample がありません" in reason


class TestZeroTargetMetrics:
    """真値 0 の部分集団を絶対誤差で測る.

    ``valid_sample_mask`` は ``target > 0`` を要求するので、blank は全体 metric
    から必ず外れる。相対誤差が定義できない集団なので、専用の metric で測る。
    """

    def test_selects_only_the_samples_whose_target_is_zero(self):
        predictions = _predictions(
            mean=[0.0, 2.0, 3.0, 1.5, 4.0],
            target=[0.0, 0.0, 0.0, 6.0, 8.0],
        )

        metrics, reason = ZeroTargetMetrics.measure(predictions)

        assert reason is None
        assert metrics is not None
        assert metrics.sample_count == 3

    def test_measures_the_absolute_error_without_dividing_by_the_target(self):
        """絶対誤差・p95・厳密 0 の割合・coverage を既知の値で固定する.

        相対誤差を流用すると 0 除算で ``inf`` / ``nan`` が出る。全 field が有限
        であることと、既知の構成での厳密な値を同時に見る。
        """

        predictions = _predictions(mean=[0.0, 2.0, 3.0], target=[0.0, 0.0, 0.0])

        metrics, reason = ZeroTargetMetrics.measure(predictions)

        assert reason is None
        assert metrics is not None
        assert metrics.sample_count == 3
        assert metrics.weight_sum == pytest.approx(3.0)
        assert metrics.mean_absolute_error == pytest.approx(5.0 / 3.0)
        assert metrics.p95_absolute_error == pytest.approx(2.9)
        assert metrics.exact_zero_fraction == pytest.approx(1.0 / 3.0)
        # log 分散 0 なので standard deviation は 1。誤差 0 の 1 件だけが収まる
        assert metrics.one_standard_deviation_coverage == pytest.approx(1.0 / 3.0)
        for value in attrs.astuple(metrics):
            assert math.isfinite(value)

    def test_weights_the_absolute_error(self):
        predictions = _predictions(
            mean=[0.0, 4.0], target=[0.0, 0.0], sample_weight=[1.0, 3.0]
        )

        metrics, reason = ZeroTargetMetrics.measure(predictions)

        assert reason is None
        assert metrics is not None
        assert metrics.weight_sum == pytest.approx(4.0)
        assert metrics.mean_absolute_error == pytest.approx(3.0)

    def test_reports_a_population_without_any_zero_target(self):
        """空集団と「重みが 0」は別の理由で返す.

        件数の guard を消しても weight 合計の guard が同じ ``None`` を返すので、
        理由文を完全一致で固定する。
        """

        metrics, reason = ZeroTargetMetrics.measure(
            _predictions(mean=[1.0, 2.0], target=[1.0, 2.0])
        )

        assert metrics is None
        assert reason == "真値 0 の sample がありません"

    def test_reports_a_length_mismatch_instead_of_raising(self):
        """長さの違う予測は理由文字列で返す.

        検証を飛ばすと、選択 mask の ``&`` が broadcast できず例外になる。
        """

        metrics, reason = ZeroTargetMetrics.measure(
            GaussianPredictions(
                mean=torch.tensor([0.0, 1.0]),
                log_variance=torch.zeros(2),
                target=torch.zeros(3),
                sample_weight=torch.ones(2),
            )
        )

        assert metrics is None
        assert reason is not None
        assert "件数" in reason

    def test_a_negative_mean_is_not_an_exact_zero(self):
        """厳密 0 の割合は ``mean == 0`` の比較.

        ``mean <= 0`` へ緩めると、負の平均を「blank を当てた」と数えてしまう。
        """

        metrics, reason = ZeroTargetMetrics.measure(
            _predictions(mean=[0.0, -1.0], target=[0.0, 0.0])
        )

        assert reason is None
        assert metrics is not None
        assert metrics.sample_count == 2
        assert metrics.exact_zero_fraction == pytest.approx(0.5)

    @pytest.mark.parametrize(
        ("mean", "log_variance", "sample_weight", "expected"),
        [
            ([0.0, float("nan")], [0.0, 0.0], [1.0, 1.0], 1),
            ([0.0, 1.0], [0.0, float("inf")], [1.0, 1.0], 1),
            ([0.0, 1.0], [0.0, 0.0], [1.0, float("nan")], 1),
            ([0.0, 1.0], [0.0, 0.0], [1.0, -1.0], 1),
            ([0.0, 1.0], [0.0, 0.0], [1.0, 1.0], 2),
        ],
    )
    def test_excludes_an_unusable_sample_from_the_zero_target_population(
        self,
        mean: list[float],
        log_variance: list[float],
        sample_weight: list[float],
        expected: int,
    ):
        """非有限値と負の重みを持つ真値 0 の sample は母集団から外す.

        4 つの条件はどれか 1 つを消しても他が拾わないので、1 条件 1 ケース。
        """

        metrics, reason = ZeroTargetMetrics.measure(
            _predictions(
                mean=mean,
                target=[0.0, 0.0],
                log_variance=log_variance,
                sample_weight=sample_weight,
            )
        )

        assert reason is None
        assert metrics is not None
        assert metrics.sample_count == expected

    def test_reports_a_zero_target_population_whose_weights_sum_to_zero(self):
        metrics, reason = ZeroTargetMetrics.measure(
            _predictions(mean=[0.0, 1.0], target=[0.0, 0.0], sample_weight=[0.0, 0.0])
        )

        assert metrics is None
        assert reason is not None
        assert "weight" in reason


class TestMeanSaturationDiagnostic:
    """平均 head の ReLU が 0 に張り付いた sample の割合.

    真値 0 の飽和は正常な収束、真値が正の側の飽和が問題なので、2 つを混ぜない。
    """

    def test_counts_the_two_populations_separately(self):
        """真値正 3 件中 2 件、真値 0 の 3 件中 1 件が飽和した構成で数える.

        2 つの母集団を合算する変異は、どちらかの厳密な件数で必ず落ちる。
        """

        predictions = _predictions(
            mean=[0.0, 0.0, 2.0, 0.0, 1.0, 3.0],
            target=[5.0, 0.0, 0.0, 4.0, 2.0, 0.0],
        )

        diagnostic = MeanSaturationDiagnostic.measure(predictions)

        assert diagnostic.positive_target_count == 3
        assert diagnostic.saturated_positive_count == 2
        assert diagnostic.saturated_positive_fraction == pytest.approx(2.0 / 3.0)
        assert diagnostic.zero_target_count == 3
        assert diagnostic.saturated_zero_count == 1
        assert diagnostic.saturated_zero_fraction == pytest.approx(1.0 / 3.0)

    def test_only_an_exact_zero_counts_as_saturated(self):
        """飽和判定は ``mean == 0.0`` の厳密比較.

        Softplus の最小出力 2.4e-2 どころか 1e-12 でも飽和とは数えない。
        しきい値付きの判定へ変える変異をここで捕まえる。
        """

        predictions = _predictions(mean=[1e-12, 0.0], target=[1.0, 1.0])

        diagnostic = MeanSaturationDiagnostic.measure(predictions)

        assert diagnostic.positive_target_count == 2
        assert diagnostic.saturated_positive_count == 1

    def test_rejects_predictions_whose_lengths_disagree(self):
        """長さ不一致は broadcast させず例外にする.

        ``(n,)`` と ``(1,)`` の組は黙って broadcast し、飽和件数が母集団を上回った
        割合を返してしまう。

        実データでは起きず呼び出し側の取り違えでしか起きないので理由文字列に
        しない。
        """

        predictions = GaussianPredictions(
            mean=torch.zeros(3),
            log_variance=torch.zeros(3),
            target=torch.ones(1),
            sample_weight=torch.ones(3),
        )

        with pytest.raises(ValueError, match="件数"):
            MeanSaturationDiagnostic.measure(predictions)

    def test_a_negative_mean_is_not_counted_as_saturated(self):
        """飽和は ``mean == 0`` の厳密比較で、負の平均は含めない.

        ReLU の出力は負にならないが、``GaussianPredictions`` は他の model の
        予測も受け取れる。``mean <= 0`` へ緩める変異をここで捕まえる。
        """

        diagnostic = MeanSaturationDiagnostic.measure(
            _predictions(mean=[-1.0, 0.0], target=[1.0, 1.0])
        )

        assert diagnostic.positive_target_count == 2
        assert diagnostic.saturated_positive_count == 1

    def test_a_negative_target_belongs_to_neither_population(self):
        """真値 0 の母集団は ``target == 0`` の厳密比較.

        ``target <= 0`` へ緩めると、壊れた負の真値が blank として数えられる。
        """

        diagnostic = MeanSaturationDiagnostic.measure(
            _predictions(mean=[0.0, 0.0], target=[-1.0, 0.0])
        )

        assert diagnostic.zero_target_count == 1
        assert diagnostic.saturated_zero_count == 1
        assert diagnostic.positive_target_count == 0

    def test_a_population_without_any_zero_target_does_not_divide_by_zero(self):
        """真値 0 が 1 件も無くても 0 除算せず割合 0.0 を返す."""

        diagnostic = MeanSaturationDiagnostic.measure(
            _predictions(mean=[1.0, 2.0], target=[1.0, 2.0])
        )

        assert diagnostic.zero_target_count == 0
        assert diagnostic.saturated_zero_fraction == 0.0
        assert diagnostic.saturated_positive_fraction == 0.0

    def test_a_blank_only_population_is_not_reported_as_a_positive_problem(self):
        """Blank を全部 0 と当てても、真値正側の飽和割合は 0.0 のまま."""

        diagnostic = MeanSaturationDiagnostic.measure(
            _predictions(mean=[0.0, 0.0, 0.0], target=[0.0, 0.0, 0.0])
        )

        assert diagnostic.saturated_zero_count == 3
        assert diagnostic.saturated_zero_fraction == pytest.approx(1.0)
        assert diagnostic.positive_target_count == 0
        assert diagnostic.saturated_positive_count == 0
