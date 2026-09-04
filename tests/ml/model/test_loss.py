"""重み付き Gaussian negative log likelihood の公開契約."""

import math

import pytest
import torch

from ml.model.loss import (
    validate_gaussian_inputs,
    weighted_gaussian_negative_log_likelihood,
)

MEAN = torch.tensor([[1.0], [2.0]])
LOG_VARIANCE = torch.tensor([[0.0], [2.0]])
TARGET = torch.tensor([[1.5], [1.0]])
SAMPLE_WEIGHT = torch.tensor([[1.0], [3.0]])


class TestWeightedGaussianNegativeLogLikelihood:
    """0 次元・微分可能・重み合計で正規化された loss."""

    def test_matches_a_hand_computed_value(self):
        loss = weighted_gaussian_negative_log_likelihood(
            MEAN, LOG_VARIANCE, TARGET, SAMPLE_WEIGHT
        )

        first = 0.5 * (math.exp(-0.0) * 0.25 + 0.0)
        second = 0.5 * (math.exp(-2.0) * 1.0 + 2.0)
        expected = (1.0 * first + 3.0 * second) / 4.0
        assert loss.ndim == 0
        assert float(loss.item()) == pytest.approx(expected, rel=1e-6)

    def test_is_invariant_to_scaling_every_weight(self):
        baseline = weighted_gaussian_negative_log_likelihood(
            MEAN, LOG_VARIANCE, TARGET, SAMPLE_WEIGHT
        )
        scaled = weighted_gaussian_negative_log_likelihood(
            MEAN, LOG_VARIANCE, TARGET, SAMPLE_WEIGHT * 7.0
        )

        torch.testing.assert_close(scaled, baseline)

    def test_changes_when_the_weight_balance_changes(self):
        baseline = weighted_gaussian_negative_log_likelihood(
            MEAN, LOG_VARIANCE, TARGET, SAMPLE_WEIGHT
        )
        reweighted = weighted_gaussian_negative_log_likelihood(
            MEAN, LOG_VARIANCE, TARGET, torch.tensor([[3.0], [1.0]])
        )

        assert not bool(torch.allclose(reweighted, baseline))

    def test_ignores_samples_with_zero_weight(self):
        mean = torch.tensor([[1.0], [2.0], [9.0]])
        log_variance = torch.tensor([[0.0], [2.0], [0.0]])
        target = torch.tensor([[1.5], [1.0], [-4.0]])
        weight = torch.tensor([[1.0], [3.0], [0.0]])

        loss = weighted_gaussian_negative_log_likelihood(
            mean, log_variance, target, weight
        )

        torch.testing.assert_close(
            loss,
            weighted_gaussian_negative_log_likelihood(
                MEAN, LOG_VARIANCE, TARGET, SAMPLE_WEIGHT
            ),
        )

    def test_propagates_gradients_to_the_mean_and_the_log_variance(self):
        mean = MEAN.clone().requires_grad_(True)
        log_variance = LOG_VARIANCE.clone().requires_grad_(True)

        weighted_gaussian_negative_log_likelihood(
            mean, log_variance, TARGET, SAMPLE_WEIGHT
        ).backward()

        assert mean.grad is not None
        assert log_variance.grad is not None
        assert float(mean.grad.abs().sum().item()) > 0.0
        assert float(log_variance.grad.abs().sum().item()) > 0.0

    def test_is_minimized_where_the_log_variance_matches_the_squared_error(self):
        mean = torch.tensor([[1.0]])
        target = torch.tensor([[1.5]])
        weight = torch.tensor([[1.0]])
        optimum = math.log((1.5 - 1.0) ** 2)

        def loss_at(log_variance: float) -> float:
            return float(
                weighted_gaussian_negative_log_likelihood(
                    mean, torch.tensor([[log_variance]]), target, weight
                ).item()
            )

        assert loss_at(optimum) < loss_at(optimum - 0.5)
        assert loss_at(optimum) < loss_at(optimum + 0.5)

    @pytest.mark.parametrize("log_variance", [-14.0, 5.0])
    def test_stays_finite_at_the_clamp_boundaries(self, log_variance: float):
        loss = weighted_gaussian_negative_log_likelihood(
            torch.tensor([[1.0]]),
            torch.tensor([[log_variance]]),
            torch.tensor([[2.0]]),
            torch.tensor([[1.0]]),
        )

        assert bool(torch.isfinite(loss))

    def test_rejects_mismatched_shapes(self):
        with pytest.raises(ValueError, match="同じ shape"):
            weighted_gaussian_negative_log_likelihood(
                MEAN, LOG_VARIANCE, torch.tensor([1.5, 1.0]), SAMPLE_WEIGHT
            )


class TestValidateGaussianInputs:
    """Compile 境界の外で行う値の検査."""

    def test_accepts_usable_inputs(self):
        assert (
            validate_gaussian_inputs(MEAN, LOG_VARIANCE, TARGET, SAMPLE_WEIGHT) is None
        )

    @pytest.mark.parametrize(
        ("name", "index"),
        [("mean", 0), ("log_variance", 1), ("target", 2), ("sample_weight", 3)],
    )
    def test_rejects_non_finite_values(self, name: str, index: int):
        tensors = [
            MEAN.clone(),
            LOG_VARIANCE.clone(),
            TARGET.clone(),
            SAMPLE_WEIGHT.clone(),
        ]
        tensors[index][0, 0] = float("nan")

        assert validate_gaussian_inputs(*tensors) == f"{name} に非有限値が含まれます"

    def test_rejects_a_negative_weight(self):
        weight = torch.tensor([[1.0], [-1.0]])

        assert validate_gaussian_inputs(MEAN, LOG_VARIANCE, TARGET, weight) == (
            "sample_weight に負の値が含まれます"
        )

    def test_rejects_a_zero_weight_sum(self):
        weight = torch.zeros_like(SAMPLE_WEIGHT)

        reason = validate_gaussian_inputs(MEAN, LOG_VARIANCE, TARGET, weight)

        assert reason is not None
        assert "sample_weight の合計" in reason
