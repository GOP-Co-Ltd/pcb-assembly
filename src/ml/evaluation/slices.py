"""次元別 slice と reliability bin による回帰診断.

全体 metric だけでは、特定の機体や条件でのみ劣化する model を見逃す。

次元は名前と、予測と同じ並びの値列として渡す。

sample ID の突き合わせを持ち込まないので、``ml`` 側は sample の同一性を知らなくてよい。
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from itertools import pairwise

import attrs
import torch
from torch import Tensor

from ml.evaluation._aggregation import weighted_mean
from ml.evaluation.regression import (
    GaussianPredictions,
    GaussianRegressionMetrics,
    MeanSaturationDiagnostic,
    ZeroTargetMetrics,
)

_MINIMUM_BOUNDARY_DIGITS = 4


@attrs.frozen
class CategoricalDimension:
    """カテゴリ値で slice する次元."""

    name: str
    values: tuple[str, ...]

    def validate(self, sample_count: int) -> str | None:
        """次元名と値列が予測と対応しているかを検証する."""

        return _validate_dimension(self.name, len(self.values), sample_count)


@attrs.frozen
class NumericDimension:
    """数値を percentile で bucket 化して slice する次元."""

    name: str
    values: tuple[float, ...]
    bucket_count: int = 4

    def validate(self, sample_count: int) -> str | None:
        """次元名・値列・bucket 数の整合を検証する."""

        if error := _validate_dimension(self.name, len(self.values), sample_count):
            return error
        if self.bucket_count < 1:
            return (
                f"{self.name} の bucket_count は正の整数が必要です: {self.bucket_count}"
            )
        if any(not math.isfinite(value) for value in self.values):
            return f"{self.name} の値に非有限値があります"
        return None


type SliceDimension = CategoricalDimension | NumericDimension


@attrs.frozen
class DiagnosticSlice:
    """1 つの次元の 1 つの値に属する sample の metric.

    ``reason`` は ``metrics`` を出せなかった理由だけを持つ。
    どの slice かは ``dimension`` と ``value`` が示すので繰り返さない。
    """

    dimension: str
    value: str
    sample_count: int
    metrics: GaussianRegressionMetrics | None
    reason: str | None


@attrs.frozen
class ReliabilityBin:
    """予測 standard deviation の帯ごとの校正状況.

    帯は件数で切るが、帯の中の 3 統計は全体 metric や slice と同じ
    ``sum(w * x) / sum(w)`` で集計する。

    重みが 0 の sample は帯に入れない。
    """

    lower_standard_deviation: float
    upper_standard_deviation: float
    sample_count: int
    mean_predicted_standard_deviation: float
    observed_root_mean_squared_error: float
    one_standard_deviation_coverage: float


@attrs.frozen
class DiagnosticReport:
    """全体 metric、次元別 slice、reliability bin をまとめた診断結果.

    真値 0 の sample は全体 metric と slice から外れるので、``zero_target`` と
    ``mean_saturation`` で別に見る。
    """

    overall: GaussianRegressionMetrics
    slices: tuple[DiagnosticSlice, ...]
    reliability_bins: tuple[ReliabilityBin, ...]
    zero_target: ZeroTargetMetrics | None
    mean_saturation: MeanSaturationDiagnostic

    @classmethod
    def build(
        cls,
        predictions: GaussianPredictions,
        *,
        dimensions: Sequence[SliceDimension],
        reliability_bin_count: int = 5,
    ) -> tuple[DiagnosticReport | None, str | None]:
        """全体・次元別・reliability bin の診断をまとめて組む.

        slice は次元の指定順に並べ、次元の中では値の昇順にする。

        値列の長さが予測と食い違うのは呼び出し側の不変条件違反なので ``ValueError``。
        """

        if error := predictions.validate():
            return None, error
        sample_count = int(predictions.mean.numel())
        for dimension in dimensions:
            if error := dimension.validate(sample_count):
                raise ValueError(error)

        overall, reason = GaussianRegressionMetrics.measure(predictions)
        if overall is None:
            return None, reason
        slices = tuple(
            _slice_of(predictions, dimension.name, value, indices)
            for dimension in dimensions
            for value, indices in _buckets_of(dimension)
        )
        # 真値 0 が 1 件も無いのは診断の失敗ではないので、理由は報告へ載せない
        zero_target, _ = ZeroTargetMetrics.measure(predictions)
        return (
            cls(
                overall=overall,
                slices=slices,
                reliability_bins=_reliability_bins(predictions, reliability_bin_count),
                zero_target=zero_target,
                mean_saturation=MeanSaturationDiagnostic.measure(predictions),
            ),
            None,
        )


def _validate_dimension(name: str, value_count: int, sample_count: int) -> str | None:
    if not name:
        return "次元名が空です"
    if value_count != sample_count:
        return (
            f"{name} の値列が予測と一致しません: "
            f"{value_count} 件（予測は {sample_count} 件）"
        )
    return None


def _slice_of(
    predictions: GaussianPredictions,
    dimension: str,
    value: str,
    indices: Sequence[int],
) -> DiagnosticSlice:
    metrics, reason = GaussianRegressionMetrics.measure(_subset(predictions, indices))
    return DiagnosticSlice(
        dimension=dimension,
        value=value,
        sample_count=len(indices),
        metrics=metrics,
        reason=reason,
    )


def _subset(
    predictions: GaussianPredictions, indices: Sequence[int]
) -> GaussianPredictions:
    selector = torch.tensor(list(indices), dtype=torch.long)
    return GaussianPredictions(
        mean=predictions.mean[selector],
        log_variance=predictions.log_variance[selector],
        target=predictions.target[selector],
        sample_weight=predictions.sample_weight[selector],
    )


def _buckets_of(dimension: SliceDimension) -> list[tuple[str, list[int]]]:
    match dimension:
        case CategoricalDimension():
            return _categorical_buckets(dimension.values)
        case NumericDimension():
            return _numeric_buckets(dimension.values, dimension.bucket_count)


def _categorical_buckets(values: Sequence[str]) -> list[tuple[str, list[int]]]:
    grouped: dict[str, list[int]] = defaultdict(list)
    for index, value in enumerate(values):
        grouped[value].append(index)
    return [(value, grouped[value]) for value in sorted(grouped)]


def _numeric_buckets(
    values: Sequence[float], bucket_count: int
) -> list[tuple[str, list[int]]]:
    """区間ラベルと、その区間に入る sample の位置を返す.

    Sample が 1 件も入らない区間は落とす。

    percentile の境界は値が重複すると区間内が空になり、診断できない slice が並ぶため。
    """

    edges = _bucket_edges(values, bucket_count)
    labels = _boundary_labels(edges)
    if len(edges) < 2:
        return [(f"[{labels[0]},{labels[0]}]", list(range(len(values))))]

    buckets: list[tuple[str, list[int]]] = []
    for position, (lower, upper) in enumerate(pairwise(edges)):
        last = position == len(edges) - 2
        closing = "]" if last else ")"
        members = [
            index
            for index, value in enumerate(values)
            if lower <= value and (value <= upper if last else value < upper)
        ]
        if members:
            label = f"[{labels[position]},{labels[position + 1]}{closing}"
            buckets.append((label, members))
    return buckets


def _boundary_labels(edges: Sequence[float]) -> list[str]:
    """境界どうしが区別できる最小の桁数で整形する.

    ラベルは MR6 で slice を識別するキーになるため、同じ次元の中で一意にする。

    ``_bucket_edges`` の境界は狭義単調増加なので、float64 を丸めずに書ける
    17 桁までには必ず互いに異なる文字列になる。
    """

    digits = _MINIMUM_BOUNDARY_DIGITS
    labels = [f"{edge:.{digits}g}" for edge in edges]
    while len(set(labels)) != len(labels):
        digits += 1
        labels = [f"{edge:.{digits}g}" for edge in edges]
    return labels


def _bucket_edges(values: Sequence[float], bucket_count: int) -> list[float]:
    """等間隔 percentile で境界を作り、縮退した境界は畳んで bucket を減らす."""

    quantiles = torch.quantile(
        torch.tensor(values, dtype=torch.float64),
        torch.linspace(0.0, 1.0, bucket_count + 1, dtype=torch.float64),
    )
    edges: list[float] = []
    for boundary in quantiles.tolist():
        if not edges or boundary > edges[-1]:
            edges.append(boundary)
    return edges


def _reliability_bins(
    predictions: GaussianPredictions, bin_count: int
) -> tuple[ReliabilityBin, ...]:
    """予測 standard deviation の昇順に、およそ等件数の帯へ切って集計する.

    集計へ寄与しない重み 0 の sample は帯に入れない。

    帯の中の重み合計が 0 になる場合を作らないためでもある。
    """

    usable = predictions.valid_sample_mask() & (predictions.sample_weight > 0)
    standard_deviation, order = torch.sort(
        torch.exp(0.5 * predictions.log_variance[usable])
    )
    absolute_error = (predictions.mean[usable] - predictions.target[usable]).abs()[
        order
    ]
    weight = predictions.sample_weight[usable][order]
    total = int(standard_deviation.numel())
    if total == 0 or bin_count < 1:
        return ()

    return tuple(
        _reliability_bin(
            standard_deviation[start:end],
            absolute_error[start:end],
            weight[start:end],
        )
        for start, end in pairwise(
            _bin_edges(standard_deviation, min(bin_count, total))
        )
    )


def _reliability_bin(
    standard_deviation: Tensor, absolute_error: Tensor, weight: Tensor
) -> ReliabilityBin:
    """1 つの帯の 3 統計を ``sum(w * x) / sum(w)`` で集計する."""

    covered = (absolute_error <= standard_deviation).double()
    return ReliabilityBin(
        lower_standard_deviation=float(standard_deviation[0].item()),
        upper_standard_deviation=float(standard_deviation[-1].item()),
        sample_count=int(standard_deviation.numel()),
        mean_predicted_standard_deviation=weighted_mean(standard_deviation, weight),
        observed_root_mean_squared_error=math.sqrt(
            weighted_mean(absolute_error.square(), weight)
        ),
        one_standard_deviation_coverage=weighted_mean(covered, weight),
    )


def _bin_edges(sorted_values: Tensor, bin_count: int) -> list[int]:
    """同じ値を分断せず、およそ等件数になる境界位置を返す."""

    total = int(sorted_values.numel())
    edges = [0]
    for position in range(1, bin_count):
        index = round(position * total / bin_count)
        while index < total and bool(
            (sorted_values[index] == sorted_values[index - 1]).item()
        ):
            index += 1
        if index < total and index > edges[-1]:
            edges.append(index)
    edges.append(total)
    return edges


__all__ = [
    "CategoricalDimension",
    "DiagnosticReport",
    "DiagnosticSlice",
    "NumericDimension",
    "ReliabilityBin",
    "SliceDimension",
]
