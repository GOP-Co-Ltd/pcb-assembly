"""Gaussian 回帰の loss と、その入力検査.

loss 本体は shape 検査だけで済ませ、値の検査は別関数に分ける。

``.item()`` を伴う検査は ``torch.compile`` の graph を切る。

そのため compile 境界の外で 1 度だけ呼べる関数へ分けている。
"""

from __future__ import annotations

import torch
from torch import Tensor

_TENSOR_NAMES = ("mean", "log_variance", "target", "sample_weight")


def weighted_gaussian_negative_log_likelihood(
    mean: Tensor, log_variance: Tensor, target: Tensor, sample_weight: Tensor
) -> Tensor:
    """重み付き Gaussian negative log likelihood を 0 次元 tensor で返す.

    定数項 ``0.5 * log(2 * pi)`` は最適化に影響しないので落とす。

    重み合計で割るため、sample 数や重みの絶対値が変わっても大きさが揃う。
    """

    _reject_mismatched_shapes(mean, log_variance, target, sample_weight)
    squared_error = torch.square(target - mean)
    per_sample = 0.5 * (torch.exp(-log_variance) * squared_error + log_variance)
    return (sample_weight * per_sample).sum() / sample_weight.sum()


def validate_gaussian_inputs(
    mean: Tensor, log_variance: Tensor, target: Tensor, sample_weight: Tensor
) -> str | None:
    """Loss へ渡す前に値が使える範囲かを検証する.

    compile 境界の外で呼ぶ。
    """

    for name, tensor in zip(
        _TENSOR_NAMES, (mean, log_variance, target, sample_weight), strict=True
    ):
        if not bool(torch.isfinite(tensor).all()):
            return f"{name} に非有限値が含まれます"
    if bool((sample_weight < 0).any()):
        return "sample_weight に負の値が含まれます"
    total = float(sample_weight.sum().item())
    if total <= 0:
        return f"sample_weight の合計は正の値が必要です: {total}"
    return None


def _reject_mismatched_shapes(
    mean: Tensor, log_variance: Tensor, target: Tensor, sample_weight: Tensor
) -> None:
    shapes = [
        tuple(tensor.shape) for tensor in (mean, log_variance, target, sample_weight)
    ]
    if len(set(shapes)) != 1:
        raise ValueError(
            "mean、log_variance、target、sample_weight は同じ shape が"
            f"必要です: {shapes}"
        )


__all__ = [
    "validate_gaussian_inputs",
    "weighted_gaussian_negative_log_likelihood",
]
