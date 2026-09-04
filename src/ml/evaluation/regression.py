"""Gaussian 回帰の重み付き metric と不確かさ補正.

平均と log 分散を出す model の予測を、単位に依存しない指標へ集約する。

無効な sample は例外にせず集計から除外し、件数として残す。

一部が壊れた評価 run でも、残りの sample から診断できるようにするため。
"""

from __future__ import annotations

import math

import attrs
import torch
from torch import Tensor

from ml.evaluation._aggregation import weighted_mean

_MEDIAN_FRACTION = 0.5
_P95_FRACTION = 0.95


def _as_evaluation_vector(values: Tensor) -> Tensor:
    return values.detach().reshape(-1).double().cpu()


@attrs.frozen(eq=False)
class GaussianPredictions:
    """1 回の評価で集めた予測・実測・重み.

    4 本とも 1 次元・float64・CPU・計算グラフなしへそろえてから保持する。

    device と dtype の違いで metric が変わらないようにするため。

    Tensor は要素ごとの比較になり真偽値へ落ちないので、等価性は identity で決める。
    """

    mean: Tensor = attrs.field(converter=_as_evaluation_vector)
    log_variance: Tensor = attrs.field(converter=_as_evaluation_vector)
    target: Tensor = attrs.field(converter=_as_evaluation_vector)
    sample_weight: Tensor = attrs.field(converter=_as_evaluation_vector)

    def validate(self) -> str | None:
        """4 本の tensor が同じ長さで、空でないことを検証する."""

        counts = sorted(
            {
                int(values.numel())
                for values in (
                    self.mean,
                    self.log_variance,
                    self.target,
                    self.sample_weight,
                )
            }
        )
        if len(counts) != 1:
            return (
                "mean / log_variance / target / sample_weight の件数が"
                f"一致しません: {counts}"
            )
        if counts[0] == 0:
            return "予測が 1 件もありません"
        return None

    def valid_sample_mask(self) -> Tensor:
        """集計に使える sample を示す bool mask を返す.

        非有限値、0 以下の平均・実測、負の重みを持つ sample を無効とする。

        長さがそろっていることが前提なので、先に :meth:`validate` を通す。
        """

        return (
            torch.isfinite(self.mean)
            & torch.isfinite(self.log_variance)
            & torch.isfinite(self.target)
            & torch.isfinite(self.sample_weight)
            & (self.mean > 0)
            & (self.target > 0)
            & (self.sample_weight >= 0)
        )

    def with_log_variance_offset(self, offset: float) -> GaussianPredictions:
        """Scalar offset を log 分散へ足した予測を返す."""

        return attrs.evolve(self, log_variance=self.log_variance + offset)

    def fit_log_variance_offset(self) -> tuple[float | None, str | None]:
        """負の対数尤度を最小にする scalar log 分散 offset を返す.

        閉形式の最適解は ``log(sum(w * exp(-l) * (y - mu)**2) / sum(w))``。

        誤差が全て 0 だと offset が負の無限大へ発散するため、理由文字列を返す。
        """

        usable, reason = _usable_samples(self)
        if usable is None:
            return None, reason

        error_value = usable.target - usable.mean
        scaled = torch.exp(-usable.log_variance) * error_value.square()
        optimum = weighted_mean(scaled, usable.weight)
        if not math.isfinite(optimum) or optimum <= 0:
            return None, (
                "重み付き二乗誤差が正でないため log 分散 offset を決められません: "
                f"{optimum}"
            )
        return math.log(optimum), None


@attrs.frozen
class GaussianRegressionMetrics:
    """単位に依存しない Gaussian 回帰の重み付き metric.

    件数以外はすべて ``sum(w * x) / sum(w)`` で集計する。

    ``relative_error_score`` が主 gate の値で、相対誤差の偏りとばらつきの和。
    """

    sample_count: int
    valid_sample_count: int
    invalid_sample_count: int
    weight_sum: float
    negative_log_likelihood: float
    mean_absolute_error: float
    root_mean_squared_error: float
    relative_error_mean: float
    relative_error_standard_deviation: float
    relative_error_score: float
    median_absolute_relative_error: float
    p95_absolute_relative_error: float
    one_standard_deviation_coverage: float
    mean_predicted_standard_deviation: float

    @classmethod
    def measure(
        cls, predictions: GaussianPredictions
    ) -> tuple[GaussianRegressionMetrics | None, str | None]:
        """有効な sample だけを重み付きで集計する.

        無効な sample は除外して ``invalid_sample_count`` に数える。

        集計できるものが 1 件も残らないときだけ理由文字列を返す。
        """

        usable, reason = _usable_samples(predictions)
        if usable is None:
            return None, reason

        weight = usable.weight
        mean = usable.mean
        log_variance = usable.log_variance
        target = usable.target
        error_value = mean - target
        absolute_error = error_value.abs()
        predicted_standard_deviation = torch.exp(0.5 * log_variance)
        relative_error = error_value / target
        relative_error_mean = weighted_mean(relative_error, weight)
        relative_error_standard_deviation = math.sqrt(
            weighted_mean((relative_error - relative_error_mean).square(), weight)
        )
        absolute_relative_error = relative_error.abs()
        return (
            cls(
                sample_count=usable.sample_count,
                valid_sample_count=usable.valid_sample_count,
                invalid_sample_count=usable.sample_count - usable.valid_sample_count,
                weight_sum=usable.weight_sum,
                negative_log_likelihood=weighted_mean(
                    0.5
                    * (torch.exp(-log_variance) * error_value.square() + log_variance),
                    weight,
                ),
                mean_absolute_error=weighted_mean(absolute_error, weight),
                root_mean_squared_error=math.sqrt(
                    weighted_mean(error_value.square(), weight)
                ),
                relative_error_mean=relative_error_mean,
                relative_error_standard_deviation=relative_error_standard_deviation,
                relative_error_score=(
                    abs(relative_error_mean) + relative_error_standard_deviation
                ),
                median_absolute_relative_error=_weighted_percentile(
                    absolute_relative_error, weight, _MEDIAN_FRACTION
                ),
                p95_absolute_relative_error=_weighted_percentile(
                    absolute_relative_error, weight, _P95_FRACTION
                ),
                one_standard_deviation_coverage=weighted_mean(
                    (absolute_error <= predicted_standard_deviation).double(), weight
                ),
                mean_predicted_standard_deviation=weighted_mean(
                    predicted_standard_deviation, weight
                ),
            ),
            None,
        )


@attrs.frozen(eq=False)
class _UsableSamples:
    """集計に使える sample だけを取り出した状態.

    等価性は identity で決める（:class:`GaussianPredictions` と同じ理由）。
    """

    sample_count: int
    valid_sample_count: int
    mean: Tensor
    log_variance: Tensor
    target: Tensor
    weight: Tensor
    weight_sum: float


def _usable_samples(
    predictions: GaussianPredictions,
) -> tuple[_UsableSamples | None, str | None]:
    """検証・有効判定・weight 合計判定をまとめて通した sample を返す.

    集計できるものが 1 件も残らないときだけ理由文字列を返す。
    """

    if error := predictions.validate():
        return None, error
    valid = predictions.valid_sample_mask()
    sample_count = int(predictions.mean.numel())
    valid_sample_count = int(valid.sum().item())
    if valid_sample_count == 0:
        return None, f"有効な sample がありません: {sample_count} 件すべて無効です"

    weight = predictions.sample_weight[valid]
    weight_sum = float(weight.sum().item())
    if weight_sum <= 0:
        return None, (
            f"有効 sample の weight 合計が 0 です: 有効 {valid_sample_count} 件"
        )
    return (
        _UsableSamples(
            sample_count=sample_count,
            valid_sample_count=valid_sample_count,
            mean=predictions.mean[valid],
            log_variance=predictions.log_variance[valid],
            target=predictions.target[valid],
            weight=weight,
            weight_sum=weight_sum,
        ),
        None,
    )


def _weighted_percentile(values: Tensor, weight: Tensor, fraction: float) -> float:
    """重み付き percentile を線形補間で求める.

    昇順に並べた累積重み ``C_i`` に対し、位置を ``(C_i - w_i) / (W - w_i)``
    と定義する。

    重みが一様なら位置は ``i / (n - 1)`` となり、``torch.quantile`` の
    ``interpolation="linear"`` と一致する。

    この定義は「重み = 複製回数」と読み替えた場合とは一致しない。

    整数重みの sample を複製した集合の percentile とは別の値になる。

    他の metric が持つ複製等価性より、一様重みで ``torch.quantile`` に
    一致する側を契約に採る。

    重みが正の sample が 1 件以上あることを前提とする。

    加えて、重みの比が float64 の分解能内であることを前提とする。
    ある sample の重みが合計に対して無視できるほど小さいと位置の分母が 0 になり、
    戻り値が ``nan`` になる。実際の重みは session 内 pad 数と pad 内 view 数の
    逆数なので、比は高々 1e4 程度に収まる。
    """

    positive = weight > 0
    sorted_values, order = torch.sort(values[positive])
    sorted_weight = weight[positive][order]
    if sorted_values.numel() == 1:
        return float(sorted_values[0].item())

    total = sorted_weight.sum()
    exclusive = torch.cumsum(sorted_weight, dim=0) - sorted_weight
    positions = exclusive / (total - sorted_weight)
    upper = int(
        torch.searchsorted(
            positions, torch.tensor(fraction, dtype=positions.dtype), right=True
        ).item()
    )
    if upper == 0:
        return float(sorted_values[0].item())
    if upper >= positions.numel():
        return float(sorted_values[-1].item())
    span = float((positions[upper] - positions[upper - 1]).item())
    ratio = (fraction - float(positions[upper - 1].item())) / span
    lower_value = float(sorted_values[upper - 1].item())
    return lower_value + ratio * (float(sorted_values[upper].item()) - lower_value)


__all__ = [
    "GaussianPredictions",
    "GaussianRegressionMetrics",
]
