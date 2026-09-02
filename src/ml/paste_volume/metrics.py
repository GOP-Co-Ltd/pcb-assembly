"""Paste-volume Gaussian回帰metricと評価集計."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass
from typing import Literal

import torch
from torch import Tensor

from ml.evaluation.regression import (
    fit_gaussian_log_variance_offset,
    gaussian_regression_metrics,
)
from ml.training.loop import execute_evaluation_batches

from .model import validate_model_inputs
from .training_types import TrainingBatch, TrainingData


@dataclass(frozen=True)
class RegressionMetrics:
    gaussian_nll: float
    mae_ul: float
    rmse_ul: float
    normalized_error_mean: float
    normalized_error_std: float
    normalized_error_score: float
    median_absolute_relative_error: float
    p95_absolute_relative_error: float
    one_std_coverage: float
    mean_prediction_std_ul: float
    invalid_prediction_count: int
    sample_count: int

    def to_dict(self, prefix: str = "") -> dict[str, float]:
        values = asdict(self)
        return {f"{prefix}{key}": float(value) for key, value in values.items()}


def _prediction_metrics(
    mean: Tensor,
    log_variance: Tensor,
    target: Tensor,
    weights: Tensor,
) -> RegressionMetrics:
    metrics = gaussian_regression_metrics(mean, log_variance, target, weights)
    return RegressionMetrics(
        gaussian_nll=metrics.gaussian_nll,
        mae_ul=metrics.mae,
        rmse_ul=metrics.rmse,
        normalized_error_mean=metrics.normalized_error_mean,
        normalized_error_std=metrics.normalized_error_std,
        normalized_error_score=metrics.normalized_error_score,
        median_absolute_relative_error=metrics.median_absolute_relative_error,
        p95_absolute_relative_error=metrics.p95_absolute_relative_error,
        one_std_coverage=metrics.one_std_coverage,
        mean_prediction_std_ul=metrics.mean_prediction_std,
        invalid_prediction_count=metrics.invalid_prediction_count,
        sample_count=metrics.sample_count,
    )


@dataclass(frozen=True)
class EvaluationPredictions:
    metrics: RegressionMetrics
    mean_volume_ul: Tensor
    log_variance_volume_ul2: Tensor
    target_volume_ul: Tensor
    sample_weight: Tensor
    sample_ids: tuple[str, ...]


@dataclass(frozen=True)
class _EvaluationObservation:
    mean: Tensor
    log_variance: Tensor
    target: Tensor
    sample_weight: Tensor
    sample_ids: tuple[str, ...]


@dataclass(frozen=True)
class TrainingObservation:
    mean: Tensor
    log_variance: Tensor
    target: Tensor
    sample_weight: Tensor


class RegressionMetricReducer:
    def __init__(self) -> None:
        self._means: list[Tensor] = []
        self._log_variances: list[Tensor] = []
        self._targets: list[Tensor] = []
        self._weights: list[Tensor] = []

    def update(self, observations: Sequence[TrainingObservation]) -> None:
        self._means.extend(item.mean for item in observations)
        self._log_variances.extend(item.log_variance for item in observations)
        self._targets.extend(item.target for item in observations)
        self._weights.extend(item.sample_weight for item in observations)

    def metrics(self) -> RegressionMetrics | None:
        if not self._means:
            return None
        return _prediction_metrics(
            torch.cat(self._means),
            torch.cat(self._log_variances),
            torch.cat(self._targets),
            torch.cat(self._weights),
        )


def evaluate_batches(
    model_forward: Callable[[Tensor, Tensor, Tensor], tuple[Tensor, Tensor]],
    data: TrainingData,
    *,
    split: Literal["validation", "test"],
    device: torch.device,
    log_variance_offset: float = 0.0,
    deadline_monotonic: float | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> EvaluationPredictions:
    """指定splitを一度評価し、metricと全predictionを返す.

    ``stop_requested`` はbatch境界で評価を止める。実行中のbatchで
    requestが立った場合も、次のbatchはmaterializeしない。
    """

    def batch_factories() -> Iterator[Callable[[], TrainingBatch]]:
        for batch_ids in data.evaluation_batch_plan(split):
            yield lambda batch_ids=batch_ids: data.evaluation_batch(
                batch_ids, split=split
            ).to(device)

    def evaluate_batch(batch: TrainingBatch) -> _EvaluationObservation:
        validate_model_inputs(
            batch.image_6ch, batch.valid_pixel_mask, batch.pixel_per_mm
        )
        mean, log_variance = model_forward(
            batch.image_6ch, batch.valid_pixel_mask, batch.pixel_per_mm
        )
        return _EvaluationObservation(
            mean=mean.cpu(),
            log_variance=(log_variance + log_variance_offset).cpu(),
            target=batch.target_volume_ul.cpu(),
            sample_weight=batch.sample_weight.cpu(),
            sample_ids=batch.sample_ids,
        )

    observations = execute_evaluation_batches(
        batch_factories(),
        step=evaluate_batch,
        deadline_monotonic=deadline_monotonic,
        stop_requested=stop_requested,
    )
    if not observations:
        raise ValueError(f"{split} split has no batches")
    all_mean = torch.cat([item.mean for item in observations])
    all_log_variance = torch.cat([item.log_variance for item in observations])
    all_targets = torch.cat([item.target for item in observations])
    all_weights = torch.cat([item.sample_weight for item in observations])
    metrics = _prediction_metrics(
        all_mean,
        all_log_variance,
        all_targets,
        all_weights,
    )
    return EvaluationPredictions(
        metrics=metrics,
        mean_volume_ul=all_mean,
        log_variance_volume_ul2=all_log_variance,
        target_volume_ul=all_targets,
        sample_weight=all_weights,
        sample_ids=tuple(
            sample_id for item in observations for sample_id in item.sample_ids
        ),
    )


def fit_log_variance_offset(predictions: EvaluationPredictions) -> float:
    """ValidationだけからGaussian NLL最適なscalar log-variance offsetを求める."""

    return fit_gaussian_log_variance_offset(
        predictions.mean_volume_ul,
        predictions.log_variance_volume_ul2,
        predictions.target_volume_ul,
        predictions.sample_weight,
    )


__all__ = [
    "EvaluationPredictions",
    "RegressionMetricReducer",
    "RegressionMetrics",
    "TrainingObservation",
    "evaluate_batches",
    "fit_log_variance_offset",
]
