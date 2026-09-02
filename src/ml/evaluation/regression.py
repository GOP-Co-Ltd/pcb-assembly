"""Gaussian回帰に共通するmetricと不確かさ補正."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import torch
from torch import Tensor


@dataclass(frozen=True)
class GaussianRegressionMetrics:
    """単位に依存しないweighted Gaussian回帰metric."""

    gaussian_nll: float
    mae: float
    rmse: float
    normalized_error_mean: float
    normalized_error_std: float
    normalized_error_score: float
    median_absolute_relative_error: float
    p95_absolute_relative_error: float
    one_std_coverage: float
    mean_prediction_std: float
    invalid_prediction_count: int
    sample_count: int

    def to_dict(self, prefix: str = "") -> dict[str, float]:
        values = asdict(self)
        return {f"{prefix}{key}": float(value) for key, value in values.items()}


def gaussian_regression_metrics(
    mean: Tensor,
    log_variance: Tensor,
    target: Tensor,
    sample_weight: Tensor,
) -> GaussianRegressionMetrics:
    """Gaussian回帰のweighted metricをCPU上で集計する."""

    mean = mean.detach().double().flatten().cpu()
    log_variance = log_variance.detach().double().flatten().cpu()
    target = target.detach().double().flatten().cpu()
    sample_weight = sample_weight.detach().double().flatten().cpu()
    finite = (
        torch.isfinite(mean)
        & torch.isfinite(log_variance)
        & torch.isfinite(target)
        & torch.isfinite(sample_weight)
        & (mean > 0)
        & (target > 0)
        & (sample_weight >= 0)
    )
    invalid_count = int((~finite).sum().item())
    if (
        invalid_count
        or not finite.any().item()
        or sample_weight[finite].sum().item() <= 0
    ):
        raise ValueError("predictions, targets, and weights must be valid and finite")
    error = mean - target
    weight_sum = sample_weight.sum()
    nll = (
        0.5 * (torch.exp(-log_variance) * error.square() + log_variance) * sample_weight
    ).sum() / weight_sum
    mae = (error.abs() * sample_weight).sum() / weight_sum
    rmse = torch.sqrt((error.square() * sample_weight).sum() / weight_sum)
    prediction_std = torch.sqrt(torch.exp(log_variance))
    normalized_error = error / target
    normalized_mean = (normalized_error * sample_weight).sum() / weight_sum
    normalized_variance = (
        normalized_error.sub(normalized_mean).square() * sample_weight
    ).sum() / weight_sum
    normalized_std = torch.sqrt(normalized_variance)
    relative = error.abs() / target
    coverage = (
        (error.abs() <= prediction_std).double() * sample_weight
    ).sum() / weight_sum
    return GaussianRegressionMetrics(
        gaussian_nll=float(nll.item()),
        mae=float(mae.item()),
        rmse=float(rmse.item()),
        normalized_error_mean=float(normalized_mean.item()),
        normalized_error_std=float(normalized_std.item()),
        normalized_error_score=float((normalized_mean.abs() + normalized_std).item()),
        median_absolute_relative_error=float(torch.quantile(relative, 0.5).item()),
        p95_absolute_relative_error=float(torch.quantile(relative, 0.95).item()),
        one_std_coverage=float(coverage.item()),
        mean_prediction_std=float(
            (prediction_std * sample_weight).sum().div(weight_sum).item()
        ),
        invalid_prediction_count=invalid_count,
        sample_count=int(target.numel()),
    )


def fit_gaussian_log_variance_offset(
    mean: Tensor,
    log_variance: Tensor,
    target: Tensor,
    sample_weight: Tensor,
) -> float:
    """Gaussian NLLを最小化するscalar log-variance offsetを返す."""

    error_squared = torch.square(target.double() - mean.double())
    scaled = torch.exp(-log_variance.double()) * error_squared
    weights = sample_weight.double()
    optimum = torch.sum(scaled * weights) / torch.sum(weights)
    if not torch.isfinite(optimum).item() or optimum.item() <= 0:
        raise ValueError("predictions cannot calibrate uncertainty")
    return float(torch.log(optimum).item())


__all__ = [
    "GaussianRegressionMetrics",
    "fit_gaussian_log_variance_offset",
    "gaussian_regression_metrics",
]
