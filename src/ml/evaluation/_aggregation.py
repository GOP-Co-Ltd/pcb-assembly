"""評価 metric で共有する重み付き集計.

私有 module なので、``ml.evaluation`` パッケージの公開 API は増やさない。
"""

from __future__ import annotations

from torch import Tensor


def weighted_mean(values: Tensor, weight: Tensor) -> float:
    """``sum(w * x) / sum(w)`` を返す.

    重みの合計が正であることを前提とする。
    """

    return float((values * weight).sum().div(weight.sum()).item())
