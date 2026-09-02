"""Cross-domain fold planning and reproducible paste-volume diagnostics."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from statistics import median
from typing import Literal

import attrs

from ml.data.split import build_leave_one_group_out_plan

from .data import PasteVolumeSample, SplitManifest, make_split_manifest

type CrossGroupDimension = Literal["machine", "paste_lot", "nozzle"]

DIAGNOSTIC_REPORT_KIND = "pcbasm-paste-volume-diagnostic-report"
DIAGNOSTIC_REPORT_SCHEMA_VERSION = 1
REQUIRED_DIAGNOSTIC_DIMENSIONS = (
    "target_volume_ul",
    "image_area_pixels",
    "aspect_ratio",
    "pixel_per_mm",
    "dispense_mode",
    "machine",
    "paste_lot",
    "nozzle",
    "source",
)


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
    signed_relative_error_mean: float | None
    signed_relative_error_std: float | None
    signed_relative_error_score: float | None
    median_absolute_relative_error: float | None
    p95_absolute_relative_error: float | None
    one_std_coverage: float | None
    mean_prediction_std_ul: float | None

    def __attrs_post_init__(self) -> None:
        _validate_slice_metrics(self)

    def to_dict(self) -> dict[str, object]:
        return attrs.asdict(self)

    @classmethod
    def from_dict(cls, raw: object) -> SliceMetrics:
        data = _expect_mapping(
            raw,
            {
                "sample_count",
                "valid_prediction_count",
                "invalid_prediction_count",
                "gaussian_nll",
                "mae_ul",
                "rmse_ul",
                "normalized_error_mean",
                "normalized_error_std",
                "normalized_error_score",
                "signed_relative_error_mean",
                "signed_relative_error_std",
                "signed_relative_error_score",
                "median_absolute_relative_error",
                "p95_absolute_relative_error",
                "one_std_coverage",
                "mean_prediction_std_ul",
            },
            "slice metrics",
        )
        return cls(
            sample_count=_require_int(
                data["sample_count"], "slice metrics.sample_count"
            ),
            valid_prediction_count=_require_int(
                data["valid_prediction_count"],
                "slice metrics.valid_prediction_count",
            ),
            invalid_prediction_count=_require_int(
                data["invalid_prediction_count"],
                "slice metrics.invalid_prediction_count",
            ),
            gaussian_nll=_optional_finite_float(
                data["gaussian_nll"], "slice metrics.gaussian_nll"
            ),
            mae_ul=_optional_finite_float(data["mae_ul"], "slice metrics.mae_ul"),
            rmse_ul=_optional_finite_float(data["rmse_ul"], "slice metrics.rmse_ul"),
            normalized_error_mean=_optional_finite_float(
                data["normalized_error_mean"],
                "slice metrics.normalized_error_mean",
            ),
            normalized_error_std=_optional_finite_float(
                data["normalized_error_std"], "slice metrics.normalized_error_std"
            ),
            normalized_error_score=_optional_finite_float(
                data["normalized_error_score"],
                "slice metrics.normalized_error_score",
            ),
            signed_relative_error_mean=_optional_finite_float(
                data["signed_relative_error_mean"],
                "slice metrics.signed_relative_error_mean",
            ),
            signed_relative_error_std=_optional_finite_float(
                data["signed_relative_error_std"],
                "slice metrics.signed_relative_error_std",
            ),
            signed_relative_error_score=_optional_finite_float(
                data["signed_relative_error_score"],
                "slice metrics.signed_relative_error_score",
            ),
            median_absolute_relative_error=_optional_finite_float(
                data["median_absolute_relative_error"],
                "slice metrics.median_absolute_relative_error",
            ),
            p95_absolute_relative_error=_optional_finite_float(
                data["p95_absolute_relative_error"],
                "slice metrics.p95_absolute_relative_error",
            ),
            one_std_coverage=_optional_finite_float(
                data["one_std_coverage"], "slice metrics.one_std_coverage"
            ),
            mean_prediction_std_ul=_optional_finite_float(
                data["mean_prediction_std_ul"],
                "slice metrics.mean_prediction_std_ul",
            ),
        )


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

    @classmethod
    def from_dict(cls, raw: object) -> DiagnosticSlice:
        data = _expect_mapping(raw, {"dimension", "value", "metrics"}, "slice")
        return cls(
            dimension=_require_nonempty_string(data["dimension"], "slice.dimension"),
            value=_require_nonempty_string(data["value"], "slice.value"),
            metrics=SliceMetrics.from_dict(data["metrics"]),
        )


@attrs.frozen
class ReliabilityBin:
    lower_std_ul: float
    upper_std_ul: float
    sample_count: int
    mean_predicted_std_ul: float
    observed_rmse_ul: float
    one_std_coverage: float

    def __attrs_post_init__(self) -> None:
        _validate_reliability_bin(self)

    def to_dict(self) -> dict[str, object]:
        return attrs.asdict(self)

    @classmethod
    def from_dict(cls, raw: object) -> ReliabilityBin:
        data = _expect_mapping(
            raw,
            {
                "lower_std_ul",
                "upper_std_ul",
                "sample_count",
                "mean_predicted_std_ul",
                "observed_rmse_ul",
                "one_std_coverage",
            },
            "reliability bin",
        )
        return cls(
            lower_std_ul=_require_finite_float(
                data["lower_std_ul"], "reliability bin.lower_std_ul"
            ),
            upper_std_ul=_require_finite_float(
                data["upper_std_ul"], "reliability bin.upper_std_ul"
            ),
            sample_count=_require_int(
                data["sample_count"], "reliability bin.sample_count"
            ),
            mean_predicted_std_ul=_require_finite_float(
                data["mean_predicted_std_ul"],
                "reliability bin.mean_predicted_std_ul",
            ),
            observed_rmse_ul=_require_finite_float(
                data["observed_rmse_ul"], "reliability bin.observed_rmse_ul"
            ),
            one_std_coverage=_require_finite_float(
                data["one_std_coverage"], "reliability bin.one_std_coverage"
            ),
        )


@attrs.frozen
class DiagnosticReport:
    sample_ids: tuple[str, ...]
    overall: SliceMetrics
    slices: tuple[DiagnosticSlice, ...]
    reliability_bins: tuple[ReliabilityBin, ...]
    kind: str = DIAGNOSTIC_REPORT_KIND
    schema_version: int = DIAGNOSTIC_REPORT_SCHEMA_VERSION

    def __attrs_post_init__(self) -> None:
        _validate_diagnostic_report(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "sample_ids": list(self.sample_ids),
            "overall": self.overall.to_dict(),
            "slices": [item.to_dict() for item in self.slices],
            "reliability_bins": [item.to_dict() for item in self.reliability_bins],
        }

    @classmethod
    def from_dict(cls, raw: object) -> DiagnosticReport:
        data = _expect_mapping(
            raw,
            {
                "kind",
                "schema_version",
                "sample_ids",
                "overall",
                "slices",
                "reliability_bins",
            },
            "diagnostic report",
        )
        if (
            data["kind"] != DIAGNOSTIC_REPORT_KIND
            or data["schema_version"] != DIAGNOSTIC_REPORT_SCHEMA_VERSION
        ):
            raise ValueError("diagnostic report kind/schema versionが不正です")
        sample_ids = _string_array(data["sample_ids"], "diagnostic report.sample_ids")
        slices_raw = _require_list(data["slices"], "diagnostic report.slices")
        bins_raw = _require_list(
            data["reliability_bins"], "diagnostic report.reliability_bins"
        )
        return cls(
            sample_ids=sample_ids,
            overall=SliceMetrics.from_dict(data["overall"]),
            slices=tuple(DiagnosticSlice.from_dict(item) for item in slices_raw),
            reliability_bins=tuple(ReliabilityBin.from_dict(item) for item in bins_raw),
        )

    @classmethod
    def load(cls, path: Path) -> DiagnosticReport:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(
                f"diagnostic reportを読み込めません: {path}: {error}"
            ) from error
        return cls.from_dict(raw)


def _expect_mapping(
    raw: object, expected_keys: set[str], context: str
) -> Mapping[str, object]:
    if not isinstance(raw, Mapping) or any(type(key) is not str for key in raw):
        raise ValueError(f"{context}はmappingが必要です")
    actual = set(raw)
    if actual != expected_keys:
        raise ValueError(
            f"{context} keysが不正です: "
            f"missing={sorted(expected_keys - actual)}, "
            f"unknown={sorted(actual - expected_keys)}"
        )
    return raw


def _require_list(raw: object, context: str) -> list[object]:
    if not isinstance(raw, list):
        raise ValueError(f"{context}はarrayが必要です")
    return raw


def _require_nonempty_string(raw: object, context: str) -> str:
    if type(raw) is not str or not raw:
        raise ValueError(f"{context}は空でないstringが必要です")
    return raw


def _string_array(raw: object, context: str) -> tuple[str, ...]:
    values = _require_list(raw, context)
    if any(type(item) is not str or not item for item in values):
        raise ValueError(f"{context}は空でないstring arrayが必要です")
    return tuple(str(item) for item in values)


def _require_int(raw: object, context: str) -> int:
    if type(raw) is not int:
        raise ValueError(f"{context}はintegerが必要です")
    return raw


def _require_finite_float(raw: object, context: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{context}は有限numberが必要です")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"{context}は有限numberが必要です")
    return value


def _optional_finite_float(raw: object, context: str) -> float | None:
    if raw is None:
        return None
    return _require_finite_float(raw, context)


def _validate_slice_metrics(metrics: SliceMetrics) -> None:
    counts = (
        metrics.sample_count,
        metrics.valid_prediction_count,
        metrics.invalid_prediction_count,
    )
    if any(type(value) is not int or value < 0 for value in counts):
        raise ValueError("slice metrics countは0以上のintegerが必要です")
    if (
        metrics.valid_prediction_count + metrics.invalid_prediction_count
        != metrics.sample_count
    ):
        raise ValueError(
            "slice metrics valid/invalid countがsample_countと一致しません"
        )
    values = (
        metrics.gaussian_nll,
        metrics.mae_ul,
        metrics.rmse_ul,
        metrics.normalized_error_mean,
        metrics.normalized_error_std,
        metrics.normalized_error_score,
        metrics.signed_relative_error_mean,
        metrics.signed_relative_error_std,
        metrics.signed_relative_error_score,
        metrics.median_absolute_relative_error,
        metrics.p95_absolute_relative_error,
        metrics.one_std_coverage,
        metrics.mean_prediction_std_ul,
    )
    if metrics.valid_prediction_count == 0:
        if any(value is not None for value in values):
            raise ValueError(
                "valid predictionがないslice metricsはmetricをnullにします"
            )
        return
    if any(value is None or not math.isfinite(value) for value in values):
        raise ValueError("valid predictionを持つslice metricsは有限metricが必要です")
    assert metrics.mae_ul is not None
    assert metrics.rmse_ul is not None
    assert metrics.normalized_error_std is not None
    assert metrics.normalized_error_mean is not None
    assert metrics.normalized_error_score is not None
    assert metrics.signed_relative_error_mean is not None
    assert metrics.signed_relative_error_std is not None
    assert metrics.signed_relative_error_score is not None
    assert metrics.median_absolute_relative_error is not None
    assert metrics.p95_absolute_relative_error is not None
    assert metrics.one_std_coverage is not None
    assert metrics.mean_prediction_std_ul is not None
    if (
        min(
            metrics.mae_ul,
            metrics.rmse_ul,
            metrics.normalized_error_std,
            metrics.signed_relative_error_std,
            metrics.median_absolute_relative_error,
            metrics.p95_absolute_relative_error,
            metrics.mean_prediction_std_ul,
        )
        < 0
    ):
        raise ValueError("slice metricsのscale/count metricは0以上が必要です")
    if metrics.mean_prediction_std_ul <= 0:
        raise ValueError("mean_prediction_std_ulは正の値が必要です")
    if not 0 <= metrics.one_std_coverage <= 1:
        raise ValueError("one_std_coverageは0以上1以下が必要です")
    if metrics.p95_absolute_relative_error < metrics.median_absolute_relative_error:
        raise ValueError("relative error percentileの順序が不正です")
    if not math.isclose(
        metrics.normalized_error_score,
        abs(metrics.normalized_error_mean) + metrics.normalized_error_std,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise ValueError("normalized_error_scoreがmean/stdと一致しません")
    if not math.isclose(
        metrics.signed_relative_error_score,
        abs(metrics.signed_relative_error_mean) + metrics.signed_relative_error_std,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise ValueError("signed_relative_error_scoreがprimary定義と一致しません")


def _validate_reliability_bin(item: ReliabilityBin) -> None:
    values = (
        item.lower_std_ul,
        item.upper_std_ul,
        item.mean_predicted_std_ul,
        item.observed_rmse_ul,
        item.one_std_coverage,
    )
    if type(item.sample_count) is not int or item.sample_count <= 0:
        raise ValueError("reliability bin sample_countは正のintegerが必要です")
    if not all(math.isfinite(value) for value in values):
        raise ValueError("reliability binは有限値が必要です")
    if (
        item.lower_std_ul <= 0
        or item.upper_std_ul < item.lower_std_ul
        or not item.lower_std_ul <= item.mean_predicted_std_ul <= item.upper_std_ul
    ):
        raise ValueError("reliability binのstd範囲が不正です")
    if item.observed_rmse_ul < 0 or not 0 <= item.one_std_coverage <= 1:
        raise ValueError("reliability binのRMSE/coverageが不正です")


def _validate_diagnostic_report(report: DiagnosticReport) -> None:
    if (
        report.kind != DIAGNOSTIC_REPORT_KIND
        or report.schema_version != DIAGNOSTIC_REPORT_SCHEMA_VERSION
    ):
        raise ValueError("diagnostic report kind/schema versionが不正です")
    if not report.sample_ids:
        raise ValueError("diagnostic reportにはsample IDが必要です")
    if tuple(sorted(report.sample_ids)) != report.sample_ids:
        raise ValueError("diagnostic report sample IDはsort済みである必要があります")
    if len(set(report.sample_ids)) != len(report.sample_ids):
        raise ValueError("diagnostic report sample IDが重複しています")
    if report.overall.sample_count != len(report.sample_ids):
        raise ValueError("diagnostic report sample countがsample ID数と一致しません")
    if not report.slices:
        raise ValueError("diagnostic reportにはsliceが必要です")
    rank = {name: index for index, name in enumerate(REQUIRED_DIAGNOSTIC_DIMENSIONS)}
    actual_dimensions = {item.dimension for item in report.slices}
    if actual_dimensions != set(REQUIRED_DIAGNOSTIC_DIMENSIONS):
        raise ValueError(
            "diagnostic reportのrequired dimensionが欠落または不正です: "
            f"actual={sorted(actual_dimensions)}"
        )
    keys = tuple((item.dimension, item.value) for item in report.slices)
    if len(set(keys)) != len(keys):
        raise ValueError("diagnostic report sliceが重複しています")
    expected_order = tuple(sorted(keys, key=lambda item: (rank[item[0]], item[1])))
    if keys != expected_order:
        raise ValueError("diagnostic report sliceの順序が不正です")
    for dimension in REQUIRED_DIAGNOSTIC_DIMENSIONS:
        metrics = [
            item.metrics for item in report.slices if item.dimension == dimension
        ]
        if any(item.sample_count <= 0 for item in metrics):
            raise ValueError(f"diagnostic {dimension} sliceは空にできません")
        if sum(item.sample_count for item in metrics) != report.overall.sample_count:
            raise ValueError(f"diagnostic {dimension} sliceのsample coverageが不正です")
        if (
            sum(item.valid_prediction_count for item in metrics)
            != report.overall.valid_prediction_count
            or sum(item.invalid_prediction_count for item in metrics)
            != report.overall.invalid_prediction_count
        ):
            raise ValueError(
                f"diagnostic {dimension} sliceのprediction countが不正です"
            )
    if report.overall.valid_prediction_count == 0:
        if report.reliability_bins:
            raise ValueError("valid predictionなしではreliability binを持てません")
        return
    if not report.reliability_bins:
        raise ValueError("valid predictionにはreliability binが必要です")
    if len(report.reliability_bins) > 5:
        raise ValueError("reliability binは最大5個です")
    bin_ranges = tuple(
        (item.lower_std_ul, item.upper_std_ul) for item in report.reliability_bins
    )
    if len(set(bin_ranges)) != len(bin_ranges):
        raise ValueError("reliability binが重複しています")
    if (
        sum(item.sample_count for item in report.reliability_bins)
        != report.overall.valid_prediction_count
    ):
        raise ValueError("reliability binのsample coverageが不正です")
    previous_upper = -math.inf
    for item in report.reliability_bins:
        if item.lower_std_ul <= previous_upper:
            raise ValueError("reliability binの順序または範囲が重複しています")
        previous_upper = item.upper_std_ul


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
        return cross_group_fold_id(self.dimension, self.held_out_group)

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


def cross_group_fold_id(dimension: CrossGroupDimension, held_out_group: str) -> str:
    """Return a readable, deterministic, collision-resistant fold
    identifier."""

    if dimension not in ("machine", "paste_lot", "nozzle"):
        raise ValueError("cross-group dimensionが不正です")
    if not held_out_group:
        raise ValueError("held-out groupが必要です")
    safe_group = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in held_out_group
    ).strip("-")
    canonical = json.dumps(
        {"dimension": dimension, "held_out_group": held_out_group},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    suffix = hashlib.sha256(canonical).hexdigest()[:12]
    return f"{dimension}-{safe_group or 'group'}-{suffix}"


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
    assignment_plan = build_leave_one_group_out_plan(
        session_groups,
        dimension=dimension,
        seed=seed,
    )
    if not assignment_plan.available:
        return CrossGroupPlan(dimension, False, assignment_plan.reason, ())
    folds: list[CrossGroupFold] = []
    for assignment in assignment_plan.folds:
        train_sessions = set(assignment.train_group_ids)
        validation_sessions = set(assignment.validation_group_ids)
        held_out_sessions = set(assignment.held_out_group_ids)
        split = make_split_manifest(
            samples=samples,
            composite_fingerprint=composite_fingerprint,
            seed=assignment.seed,
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
                if sample.session_id in held_out_sessions
            ),
        )
        folds.append(
            CrossGroupFold(
                dimension=dimension,
                held_out_group=assignment.held_out_group,
                train_group_values=assignment.train_group_values,
                train_session_ids=tuple(sorted(train_sessions)),
                validation_session_ids=tuple(sorted(validation_sessions)),
                held_out_session_ids=tuple(sorted(held_out_sessions)),
                split=split,
            )
        )
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
    if any(
        not math.isfinite(sample.measured_volume_ul) or sample.measured_volume_ul <= 0
        for sample in samples
    ):
        raise ValueError("diagnostic target volumeは正の有限値が必要です")
    valid: list[tuple[float, float, float]] = []
    invalid = 0
    for sample in samples:
        prediction = predictions.get(sample.sample_id)
        if (
            prediction is None
            or not math.isfinite(prediction.mean_volume_ul)
            or prediction.mean_volume_ul <= 0
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
            signed_relative_error_mean=None,
            signed_relative_error_std=None,
            signed_relative_error_score=None,
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
    signed_relative = [
        error / target for error, (target, _, _) in zip(errors, valid, strict=True)
    ]
    normalized_mean = sum(normalized) / len(normalized)
    normalized_variance = sum(
        (value - normalized_mean) ** 2 for value in normalized
    ) / len(normalized)
    normalized_std = math.sqrt(normalized_variance)
    signed_relative_mean = sum(signed_relative) / len(signed_relative)
    signed_relative_variance = sum(
        (value - signed_relative_mean) ** 2 for value in signed_relative
    ) / len(signed_relative)
    signed_relative_std = math.sqrt(signed_relative_variance)
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
        signed_relative_error_mean=signed_relative_mean,
        signed_relative_error_std=signed_relative_std,
        signed_relative_error_score=abs(signed_relative_mean) + signed_relative_std,
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
        and prediction.mean_volume_ul > 0
    ]
    records.sort()
    if not records:
        return ()
    records_by_std: list[list[tuple[float, float]]] = []
    for record in records:
        if not records_by_std or records_by_std[-1][0][0] != record[0]:
            records_by_std.append([])
        records_by_std[-1].append(record)
    target_bin_size = max(1, math.ceil(len(records) / bin_count))
    chunks: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    for records_with_same_std in records_by_std:
        current.extend(records_with_same_std)
        if len(current) >= target_bin_size:
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    bins: list[ReliabilityBin] = []
    for chunk in chunks:
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
    sample_ids = tuple(sorted(sample.sample_id for sample in samples))
    if len(set(sample_ids)) != len(sample_ids):
        raise ValueError("diagnostic sample_idが重複しています")
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
    rank = {name: index for index, name in enumerate(REQUIRED_DIAGNOSTIC_DIMENSIONS)}
    slices = tuple(
        sorted(
            (
                DiagnosticSlice(
                    dimension, value, calculate_slice_metrics(selected, by_id)
                )
                for dimension, assignments in definitions
                for value, selected in assignments
            ),
            key=lambda item: (rank[item.dimension], item.value),
        )
    )
    return DiagnosticReport(
        sample_ids=sample_ids,
        overall=calculate_slice_metrics(samples, by_id),
        slices=slices,
        reliability_bins=_reliability_bins(samples, by_id),
    )


def load_diagnostic_report(path: Path) -> DiagnosticReport:
    """Persisted diagnostic reportをstrict schemaで読み込む."""

    return DiagnosticReport.load(path)


__all__ = [
    "CrossGroupDimension",
    "CrossGroupFold",
    "CrossGroupPlan",
    "DIAGNOSTIC_REPORT_KIND",
    "DIAGNOSTIC_REPORT_SCHEMA_VERSION",
    "DiagnosticReport",
    "DiagnosticSlice",
    "Prediction",
    "ReliabilityBin",
    "REQUIRED_DIAGNOSTIC_DIMENSIONS",
    "SliceMetrics",
    "build_cross_group_plan",
    "build_cross_group_plans",
    "build_diagnostic_report",
    "calculate_slice_metrics",
    "cross_group_fold_id",
    "load_diagnostic_report",
]
