"""Gaussian回帰modelに共通するlossと入力validation."""

from __future__ import annotations

import torch
from torch import Tensor


def weighted_gaussian_nll(
    mean: Tensor,
    log_variance: Tensor,
    target: Tensor,
    sample_weight: Tensor,
) -> Tensor:
    """Sample weightの合計で正規化したGaussian NLLを返す."""

    tensors = (mean, log_variance, target, sample_weight)
    if any(tensor.shape != mean.shape for tensor in tensors[1:]):
        raise ValueError("mean, log-variance, target, and weight shapes must match")
    squared_error = torch.square(target - mean)
    per_sample = 0.5 * (torch.exp(-log_variance) * squared_error + log_variance)
    return torch.sum(per_sample * sample_weight) / sample_weight.sum()


def validate_gaussian_nll_inputs(
    mean: Tensor,
    log_variance: Tensor,
    target: Tensor,
    sample_weight: Tensor,
) -> None:
    """Compile境界の外でGaussian NLL入力値を検証する."""

    tensors = (mean, log_variance, target, sample_weight)
    if not all(torch.all(torch.isfinite(tensor)).item() for tensor in tensors):
        raise ValueError("loss inputs must be finite")
    if torch.any(sample_weight < 0).item() or sample_weight.sum().item() <= 0:
        raise ValueError("sample weights must be non-negative with a positive sum")


__all__ = [
    "validate_gaussian_nll_inputs",
    "weighted_gaussian_nll",
]
