"""Cross-domain fold planning and reproducible paste-volume diagnostics."""

from __future__ import annotations

import hashlib
import math
import random
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from statistics import median
from typing import Literal

import attrs

from .data import PasteVolumeSample, SplitManifest, make_split_manifest

type CrossGroupDimension = Literal["machine", "paste_lot", "nozzle"]


@attrs.frozen
class Prediction:
    sample_id: str
    mean_volume_ul: float
    std_volume_ul: float


@attrs.frozen
class SliceMetrics:
    sample_count: int
    valid_prediction_count: int
    invalid_prediction_count: int
    gaussian_nll: float | None
    mae_ul: float | None
    rmse_ul: float | None
    normalized_error_mean: float | None
    normalized_error_std: float | None
    normalized_error_score: float | None
    median_absolute_relative_error: float | None
    p95_absolute_relative_error: float | None
    one_std_coverage: float | None
    mean_prediction_std_ul: float | None

    def to_dict(self) -> dict[str, object]:
        return attrs.asdict(self)


@attrs.frozen
class DiagnosticSlice:
    dimension: str
    value: str
    metrics: SliceMetrics

    def to_dict(self) -> dict[str, object]:
        return {
            "dimension": self.dimension,
            "value": self.value,
            "metrics": self.metrics.to_dict(),
        }


@attrs.frozen
class ReliabilityBin:
    lower_std_ul: float
    upper_std_ul: float
    sample_count: int
    mean_predicted_std_ul: float
    observed_rmse_ul: float
    one_std_coverage: float

    def to_dict(self) -> dict[str, object]:
        return attrs.asdict(self)


@attrs.frozen
class DiagnosticReport:
    overall: SliceMetrics
    slices: tuple[DiagnosticSlice, ...]
    reliability_bins: tuple[ReliabilityBin, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "overall": self.overall.to_dict(),
            "slices": [item.to_dict() for item in self.slices],
            "reliability_bins": [item.to_dict() for item in self.reliability_bins],
        }


@attrs.frozen
class CrossGroupFold:
    dimension: CrossGroupDimension
    held_out_group: str
    train_group_values: tuple[str, ...]
    train_session_ids: tuple[str, ...]
    validation_session_ids: tuple[str, ...]
    held_out_session_ids: tuple[str, ...]
    split: SplitManifest

    @property
    def fold_id(self) -> str:
        safe_group = "".join(
            character if character.isalnum() or character in "-_" else "-"
            for character in self.held_out_group
        ).strip("-")
        return f"{self.dimension}-{safe_group or 'group'}"

    def to_dict(self) -> dict[str, object]:
        return {
            "dimension": self.dimension,
            "held_out_group": self.held_out_group,
            "train_group_values": list(self.train_group_values),
            "train_session_ids": list(self.train_session_ids),
            "validation_session_ids": list(self.validation_session_ids),
            "held_out_session_ids": list(self.held_out_session_ids),
            "split": self.split.to_dict(),
        }


@attrs.frozen
class CrossGroupPlan:
    dimension: CrossGroupDimension
    available: bool
    reason: str | None
    folds: tuple[CrossGroupFold, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "dimension": self.dimension,
            "available": self.available,
            "reason": self.reason,
            "folds": [fold.to_dict() for fold in self.folds],
        }


def _group_value(sample: PasteVolumeSample, dimension: CrossGroupDimension) -> str:
    if dimension == "machine":
        return sample.machine_id
    if dimension == "paste_lot":
        return f"{sample.paste_id}:{sample.paste_lot or '<none>'}"
    return f"{sample.nozzle_diameter_mm:.17g}"


def build_cross_group_plan(
    samples: Sequence[PasteVolumeSample],
    composite_fingerprint: str,
    dimension: CrossGroupDimension,
    *,
    seed: int = 42,
) -> CrossGroupPlan:
    """Build true held-out group folds, never an image-level fallback."""

    if not samples:
        return CrossGroupPlan(dimension, False, "sample index is empty", ())
    session_groups: dict[str, str] = {}
    for sample in samples:
        value = _group_value(sample, dimension)
        previous = session_groups.setdefault(sample.session_id, value)
        if previous != value:
            raise ValueError(
                f"1 session内で{dimension} groupが混在しています: {sample.session_id}"
            )
    groups = sorted(set(session_groups.values()))
    if len(groups) < 2:
        return CrossGroupPlan(
            dimension,
            False,
            f"{dimension} groupが2種類未満のためleave-one-group-out評価不能です",
            (),
        )
    folds: list[CrossGroupFold] = []
    unavailable_reasons: list[str] = []
    for group in groups:
        held_out_sessions = sorted(
            session_id for session_id, value in session_groups.items() if value == group
        )
        remaining_sessions = sorted(set(session_groups) - set(held_out_sessions))
        if len(remaining_sessions) < 2:
            unavailable_reasons.append(
                f"held-out {group!r}後にtrain/validation用sessionが2個未満です"
            )
            continue
        fold_seed = int.from_bytes(
            hashlib.sha256(f"{seed}:{dimension}:{group}".encode()).digest()[:8],
            "big",
        )
        random.Random(fold_seed).shuffle(remaining_sessions)
        validation_count = max(1, round(len(remaining_sessions) * 0.15))
        if validation_count >= len(remaining_sessions):
            validation_count = 1
        validation_sessions = set(remaining_sessions[:validation_count])
        train_sessions = set(remaining_sessions[validation_count:])
        held_out_session_set = set(held_out_sessions)
        split = make_split_manifest(
            samples=samples,
            composite_fingerprint=composite_fingerprint,
            seed=fold_seed,
            mode="base",
            train_sample_ids=(
                sample.sample_id
                for sample in samples
                if sample.session_id in train_sessions
            ),
            validation_sample_ids=(
                sample.sample_id
                for sample in samples
                if sample.session_id in validation_sessions
            ),
            test_sample_ids=(
                sample.sample_id
                for sample in samples
                if sample.session_id in held_out_session_set
            ),
        )
        folds.append(
            CrossGroupFold(
                dimension=dimension,
                held_out_group=group,
                train_group_values=tuple(sorted(set(groups) - {group})),
                train_session_ids=tuple(sorted(train_sessions)),
                validation_session_ids=tuple(sorted(validation_sessions)),
                held_out_session_ids=tuple(held_out_sessions),
                split=split,
            )
        )
    if unavailable_reasons:
        return CrossGroupPlan(dimension, False, "; ".join(unavailable_reasons), ())
    return CrossGroupPlan(dimension, True, None, tuple(folds))


def build_cross_group_plans(
    samples: Sequence[PasteVolumeSample],
    composite_fingerprint: str,
    *,
    seed: int = 42,
) -> tuple[CrossGroupPlan, ...]:
    return tuple(
        build_cross_group_plan(samples, composite_fingerprint, dimension, seed=seed)
        for dimension in ("machine", "paste_lot", "nozzle")
    )


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def calculate_slice_metrics(
    samples: Sequence[PasteVolumeSample], predictions: Mapping[str, Prediction]
) -> SliceMetrics:
    valid: list[tuple[float, float, float]] = []
    invalid = 0
    for sample in samples:
        prediction = predictions.get(sample.sample_id)
        if (
            prediction is None
            or not math.isfinite(prediction.mean_volume_ul)
            or not math.isfinite(prediction.std_volume_ul)
            or prediction.std_volume_ul <= 0
        ):
            invalid += 1
            continue
        valid.append(
            (
                sample.measured_volume_ul,
                prediction.mean_volume_ul,
                prediction.std_volume_ul,
            )
        )
    if not valid:
        return SliceMetrics(
            sample_count=len(samples),
            valid_prediction_count=0,
            invalid_prediction_count=invalid,
            gaussian_nll=None,
            mae_ul=None,
            rmse_ul=None,
            normalized_error_mean=None,
            normalized_error_std=None,
            normalized_error_score=None,
            median_absolute_relative_error=None,
            p95_absolute_relative_error=None,
            one_std_coverage=None,
            mean_prediction_std_ul=None,
        )
    errors = [predicted - target for target, predicted, _ in valid]
    normalized = [error / std for error, (_, _, std) in zip(errors, valid, strict=True)]
    relative = [
        abs(error) / target for error, (target, _, _) in zip(errors, valid, strict=True)
    ]
    normalized_mean = sum(normalized) / len(normalized)
    normalized_variance = sum(
        (value - normalized_mean) ** 2 for value in normalized
    ) / len(normalized)
    normalized_std = math.sqrt(normalized_variance)
    return SliceMetrics(
        sample_count=len(samples),
        valid_prediction_count=len(valid),
        invalid_prediction_count=invalid,
        gaussian_nll=sum(
            0.5 * ((error / std) ** 2 + 2 * math.log(std))
            for error, (_, _, std) in zip(errors, valid, strict=True)
        )
        / len(valid),
        mae_ul=sum(abs(error) for error in errors) / len(errors),
        rmse_ul=math.sqrt(sum(error**2 for error in errors) / len(errors)),
        normalized_error_mean=normalized_mean,
        normalized_error_std=normalized_std,
        normalized_error_score=abs(normalized_mean) + normalized_std,
        median_absolute_relative_error=median(relative),
        p95_absolute_relative_error=_percentile(relative, 0.95),
        one_std_coverage=sum(
            abs(error) <= std for error, (_, _, std) in zip(errors, valid, strict=True)
        )
        / len(valid),
        mean_prediction_std_ul=sum(std for _, _, std in valid) / len(valid),
    )


def _numeric_bucket_assignments(
    samples: Sequence[PasteVolumeSample],
    value_of: Callable[[PasteVolumeSample], float | int],
) -> tuple[tuple[str, tuple[PasteVolumeSample, ...]], ...]:
    values = [float(value_of(sample)) for sample in samples]
    edges = sorted(
        {_percentile(values, fraction) for fraction in (0.0, 0.25, 0.5, 0.75, 1.0)}
    )
    if len(edges) == 1:
        return ((f"[{edges[0]:.6g}]", tuple(samples)),)
    buckets: list[tuple[str, tuple[PasteVolumeSample, ...]]] = []
    for index, (lower, upper) in enumerate(zip(edges, edges[1:])):
        selected = tuple(
            sample
            for sample in samples
            if float(value_of(sample)) >= lower
            and (
                float(value_of(sample)) <= upper
                if index == len(edges) - 2
                else float(value_of(sample)) < upper
            )
        )
        if selected:
            buckets.append(
                (
                    f"[{lower:.6g},{upper:.6g}{']' if index == len(edges)-2 else ')'}",
                    selected,
                )
            )
    return tuple(buckets)


def _categorical_assignments(
    samples: Sequence[PasteVolumeSample],
    value_of: Callable[[PasteVolumeSample], object],
) -> tuple[tuple[str, tuple[PasteVolumeSample, ...]], ...]:
    grouped: dict[str, list[PasteVolumeSample]] = defaultdict(list)
    for sample in samples:
        grouped[str(value_of(sample))].append(sample)
    return tuple((key, tuple(grouped[key])) for key in sorted(grouped))


def _reliability_bins(
    samples: Sequence[PasteVolumeSample],
    predictions: Mapping[str, Prediction],
    *,
    bin_count: int = 5,
) -> tuple[ReliabilityBin, ...]:
    records = [
        (
            prediction.std_volume_ul,
            prediction.mean_volume_ul - sample.measured_volume_ul,
        )
        for sample in samples
        if (prediction := predictions.get(sample.sample_id)) is not None
        and math.isfinite(prediction.std_volume_ul)
        and prediction.std_volume_ul > 0
        and math.isfinite(prediction.mean_volume_ul)
    ]
    records.sort()
    if not records:
        return ()
    bins: list[ReliabilityBin] = []
    for start in range(0, len(records), max(1, math.ceil(len(records) / bin_count))):
        chunk = records[start : start + max(1, math.ceil(len(records) / bin_count))]
        bins.append(
            ReliabilityBin(
                lower_std_ul=chunk[0][0],
                upper_std_ul=chunk[-1][0],
                sample_count=len(chunk),
                mean_predicted_std_ul=sum(std for std, _ in chunk) / len(chunk),
                observed_rmse_ul=math.sqrt(
                    sum(error**2 for _, error in chunk) / len(chunk)
                ),
                one_std_coverage=sum(abs(error) <= std for std, error in chunk)
                / len(chunk),
            )
        )
    return tuple(bins)


def build_diagnostic_report(
    samples: Sequence[PasteVolumeSample], predictions: Iterable[Prediction]
) -> DiagnosticReport:
    if not samples:
        raise ValueError("diagnostic reportにはsampleが必要です")
    prediction_records = tuple(predictions)
    by_id = {prediction.sample_id: prediction for prediction in prediction_records}
    if len(by_id) != len(prediction_records):
        raise ValueError("prediction sample_idが重複しています")
    unknown = set(by_id) - {sample.sample_id for sample in samples}
    if unknown:
        raise ValueError(f"未知sample_idのpredictionがあります: {sorted(unknown)}")
    definitions = (
        (
            "target_volume_ul",
            _numeric_bucket_assignments(
                samples, lambda sample: sample.measured_volume_ul
            ),
        ),
        (
            "image_area_pixels",
            _numeric_bucket_assignments(
                samples, lambda sample: sample.image_area_pixels
            ),
        ),
        (
            "aspect_ratio",
            _numeric_bucket_assignments(samples, lambda sample: sample.aspect_ratio),
        ),
        (
            "pixel_per_mm",
            _numeric_bucket_assignments(samples, lambda sample: sample.pixel_per_mm),
        ),
        (
            "dispense_mode",
            _categorical_assignments(samples, lambda sample: sample.dispense_mode),
        ),
        (
            "machine",
            _categorical_assignments(samples, lambda sample: sample.machine_id),
        ),
        (
            "paste_lot",
            _categorical_assignments(
                samples,
                lambda sample: f"{sample.paste_id}:{sample.paste_lot or '<none>'}",
            ),
        ),
        (
            "nozzle",
            _categorical_assignments(
                samples, lambda sample: f"{sample.nozzle_diameter_mm:.17g}"
            ),
        ),
        (
            "source",
            _categorical_assignments(
                samples, lambda sample: sorted(sample.source_ids)[0]
            ),
        ),
    )
    slices = tuple(
        DiagnosticSlice(dimension, value, calculate_slice_metrics(selected, by_id))
        for dimension, assignments in definitions
        for value, selected in assignments
    )
    return DiagnosticReport(
        overall=calculate_slice_metrics(samples, by_id),
        slices=slices,
        reliability_bins=_reliability_bins(samples, by_id),
    )


__all__ = [
    "CrossGroupDimension",
    "CrossGroupFold",
    "CrossGroupPlan",
    "DiagnosticReport",
    "DiagnosticSlice",
    "Prediction",
    "ReliabilityBin",
    "SliceMetrics",
    "build_cross_group_plan",
    "build_cross_group_plans",
    "build_diagnostic_report",
    "calculate_slice_metrics",
]
