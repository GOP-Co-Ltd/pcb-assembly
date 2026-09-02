"""Train an independent model for every cross-domain held-out fold."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal, Protocol

from .data import build_sample_index, resolve_dataset_inputs
from .evaluate import EvaluateConfig, EvaluationResult, evaluate
from .experiment import (
    ExperimentLogger,
    MLflowExperimentLogger,
    Scalar,
    write_json_artifact,
)
from .reporting import (
    CrossGroupDimension,
    CrossGroupFold,
    DiagnosticReport,
    Prediction,
    build_cross_group_plan,
    build_diagnostic_report,
    cross_group_fold_id,
)
from .train import (
    LoggerConfig,
    TrainConfig,
    train,
    training_protocol_fingerprint,
)
from .training import RegressionMetrics, TrainResult

type CrossValidationPhase = Literal["train", "evaluate"]

CROSS_VALIDATION_REPORT_KIND = "pcbasm-paste-volume-cross-validation-report"
CROSS_VALIDATION_REPORT_SCHEMA_VERSION = 1


class CrossValidationLoggerFactory(Protocol):
    """Create a fresh formal logger for every fold and phase."""

    def create(
        self, *, phase: CrossValidationPhase, fold: CrossGroupFold
    ) -> ExperimentLogger: ...


class _FoldTaggedLogger:
    def __init__(
        self,
        delegate: ExperimentLogger,
        *,
        phase: CrossValidationPhase,
        fold: CrossGroupFold,
    ) -> None:
        self._delegate = delegate
        self._phase = phase
        self._fold = fold

    @property
    def run_id(self) -> str:
        return self._delegate.run_id

    def start(
        self,
        *,
        run_kind: str,
        run_name: str | None = None,
        tags: Mapping[str, Scalar] | None = None,
    ) -> str:
        return self._delegate.start(
            run_kind=run_kind,
            run_name=run_name,
            tags={
                **dict(tags or {}),
                "cross_validation_phase": self._phase,
                "cross_group_dimension": self._fold.dimension,
                "held_out_group": self._fold.held_out_group,
                "cross_validation_fold_id": self._fold.fold_id,
            },
        )

    def log_params(self, params: Mapping[str, Scalar]) -> None:
        self._delegate.log_params(params)

    def log_metrics(self, metrics: Mapping[str, float], *, step: int) -> None:
        self._delegate.log_metrics(metrics, step=step)

    def log_artifact(self, path: Path, *, artifact_path: str | None = None) -> None:
        self._delegate.log_artifact(path, artifact_path=artifact_path)

    def set_tags(self, tags: Mapping[str, Scalar]) -> None:
        self._delegate.set_tags(tags)

    def flush(self) -> None:
        self._delegate.flush()

    def end(self, *, status: str = "FINISHED") -> None:
        self._delegate.end(status=status)


class MLflowCrossValidationLoggerFactory:
    """MLflow logger factory with deterministic fold run names and tags."""

    def __init__(self, config: LoggerConfig) -> None:
        self._config = config

    def create(
        self, *, phase: CrossValidationPhase, fold: CrossGroupFold
    ) -> ExperimentLogger:
        prefix = self._config.run_name or "paste-volume-cross-validation"
        delegate = MLflowExperimentLogger(
            tracking_uri=self._config.tracking_uri,
            experiment_name=self._config.experiment_name,
            run_name=f"{prefix}-{fold.fold_id}-{phase}",
            metric_retry_count=self._config.metric_retry_count,
        )
        return _FoldTaggedLogger(delegate, phase=phase, fold=fold)


@dataclass(frozen=True)
class CrossValidationConfig:
    training: TrainConfig
    output_directory: Path
    dimension: CrossGroupDimension
    seed: int = 42
    evaluation_device: str = "auto"
    evaluation_compile_enabled: bool = True
    protocol_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if self.training.checkpoint.resume_checkpoint is not None:
            raise ValueError(
                "cross-validation cannot reuse one resume checkpoint across folds"
            )
        if self.evaluation_device not in ("auto", "cpu", "cuda"):
            raise ValueError("evaluation_device must be auto, cpu, or cuda")
        if self.protocol_fingerprint is not None:
            _validate_sha256(self.protocol_fingerprint, "protocol_fingerprint")


@dataclass(frozen=True)
class CrossValidationFoldResult:
    fold_id: str
    dimension: CrossGroupDimension
    held_out_group: str
    training_run_id: str
    evaluation_run_id: str
    weights_path: Path
    split_path: Path
    evaluation_report_path: Path
    diagnostic_report_path: Path
    weights_sha256: str
    split_sha256: str
    evaluation_report_sha256: str
    diagnostic_report_sha256: str
    split_fingerprint: str
    held_out_sample_ids: tuple[str, ...]
    training_best_validation_metrics: RegressionMetrics
    held_out_metrics: RegressionMetrics
    predictions: tuple[Prediction, ...]
    diagnostics: DiagnosticReport

    def __post_init__(self) -> None:
        if not self.fold_id or not self.held_out_group:
            raise ValueError("cross-validation fold ID/groupが必要です")
        if self.dimension not in ("machine", "paste_lot", "nozzle"):
            raise ValueError("cross-validation fold dimensionが不正です")
        if not self.training_run_id or not self.evaluation_run_id:
            raise ValueError("cross-validation fold run IDが必要です")
        if not self.split_fingerprint:
            raise ValueError("cross-validation fold split fingerprintが必要です")
        _validate_sha256(self.split_fingerprint, "split_fingerprint")
        if self.fold_id != cross_group_fold_id(self.dimension, self.held_out_group):
            raise ValueError("cross-validation fold IDがdimension/groupと一致しません")
        for name, value in (
            ("weights_sha256", self.weights_sha256),
            ("split_sha256", self.split_sha256),
            ("evaluation_report_sha256", self.evaluation_report_sha256),
            ("diagnostic_report_sha256", self.diagnostic_report_sha256),
        ):
            _validate_sha256(value, name)
        if not self.held_out_sample_ids:
            raise ValueError("cross-validation fold held-out sampleが必要です")
        if tuple(sorted(self.held_out_sample_ids)) != self.held_out_sample_ids:
            raise ValueError("held-out sample IDはsort済みである必要があります")
        if len(set(self.held_out_sample_ids)) != len(self.held_out_sample_ids):
            raise ValueError("held-out sample IDが重複しています")
        prediction_ids = tuple(sorted(item.sample_id for item in self.predictions))
        if prediction_ids != self.held_out_sample_ids:
            raise ValueError(
                "fold prediction sample集合がheld-out sampleと一致しません"
            )
        if any(
            not math.isfinite(item.mean_volume_ul)
            or not math.isfinite(item.std_volume_ul)
            or item.std_volume_ul <= 0
            for item in self.predictions
        ):
            raise ValueError("fold predictionは有限meanと正の有限stdが必要です")
        if self.diagnostics.sample_ids != self.held_out_sample_ids:
            raise ValueError(
                "fold diagnostics sample集合がheld-out sampleと一致しません"
            )
        if self.held_out_metrics.sample_count != len(self.held_out_sample_ids):
            raise ValueError(
                "fold held-out metrics sample countがheld-out ID数と一致しません"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "fold_id": self.fold_id,
            "dimension": self.dimension,
            "held_out_group": self.held_out_group,
            "training_run_id": self.training_run_id,
            "evaluation_run_id": self.evaluation_run_id,
            "weights_path": str(self.weights_path),
            "split_path": str(self.split_path),
            "evaluation_report_path": str(self.evaluation_report_path),
            "diagnostic_report_path": str(self.diagnostic_report_path),
            "weights_sha256": self.weights_sha256,
            "split_sha256": self.split_sha256,
            "evaluation_report_sha256": self.evaluation_report_sha256,
            "diagnostic_report_sha256": self.diagnostic_report_sha256,
            "split_fingerprint": self.split_fingerprint,
            "held_out_sample_ids": list(self.held_out_sample_ids),
            "training_best_validation_metrics": asdict(
                self.training_best_validation_metrics
            ),
            "held_out_metrics": asdict(self.held_out_metrics),
            "predictions": [
                {
                    "sample_id": prediction.sample_id,
                    "mean_volume_ul": prediction.mean_volume_ul,
                    "std_volume_ul": prediction.std_volume_ul,
                }
                for prediction in sorted(
                    self.predictions, key=lambda item: item.sample_id
                )
            ],
            "diagnostics": self.diagnostics.to_dict(),
        }

    def evidence_dict(self) -> dict[str, object]:
        payload = self.to_dict()
        for key in (
            "weights_path",
            "split_path",
            "evaluation_report_path",
            "diagnostic_report_path",
        ):
            del payload[key]
        return payload

    @classmethod
    def from_dict(cls, raw: object) -> CrossValidationFoldResult:
        data = _expect_mapping(
            raw,
            {
                "fold_id",
                "dimension",
                "held_out_group",
                "training_run_id",
                "evaluation_run_id",
                "weights_path",
                "split_path",
                "evaluation_report_path",
                "diagnostic_report_path",
                "weights_sha256",
                "split_sha256",
                "evaluation_report_sha256",
                "diagnostic_report_sha256",
                "split_fingerprint",
                "held_out_sample_ids",
                "training_best_validation_metrics",
                "held_out_metrics",
                "predictions",
                "diagnostics",
            },
            "cross-validation fold",
        )
        dimension = _cross_dimension(data["dimension"], "fold.dimension")
        prediction_raw = _require_list(data["predictions"], "fold.predictions")
        return cls(
            fold_id=_string(data["fold_id"], "fold.fold_id"),
            dimension=dimension,
            held_out_group=_string(data["held_out_group"], "fold.held_out_group"),
            training_run_id=_string(data["training_run_id"], "fold.training_run_id"),
            evaluation_run_id=_string(
                data["evaluation_run_id"], "fold.evaluation_run_id"
            ),
            weights_path=Path(_string(data["weights_path"], "fold.weights_path")),
            split_path=Path(_string(data["split_path"], "fold.split_path")),
            evaluation_report_path=Path(
                _string(data["evaluation_report_path"], "fold.evaluation_report_path")
            ),
            diagnostic_report_path=Path(
                _string(data["diagnostic_report_path"], "fold.diagnostic_report_path")
            ),
            weights_sha256=_string(data["weights_sha256"], "fold.weights_sha256"),
            split_sha256=_string(data["split_sha256"], "fold.split_sha256"),
            evaluation_report_sha256=_string(
                data["evaluation_report_sha256"], "fold.evaluation_report_sha256"
            ),
            diagnostic_report_sha256=_string(
                data["diagnostic_report_sha256"], "fold.diagnostic_report_sha256"
            ),
            split_fingerprint=_string(
                data["split_fingerprint"], "fold.split_fingerprint"
            ),
            held_out_sample_ids=_string_tuple(
                data["held_out_sample_ids"], "fold.held_out_sample_ids"
            ),
            training_best_validation_metrics=_regression_metrics_from_dict(
                data["training_best_validation_metrics"],
                "fold.training_best_validation_metrics",
            ),
            held_out_metrics=_regression_metrics_from_dict(
                data["held_out_metrics"], "fold.held_out_metrics"
            ),
            predictions=tuple(_prediction_from_dict(item) for item in prediction_raw),
            diagnostics=DiagnosticReport.from_dict(data["diagnostics"]),
        )


@dataclass(frozen=True)
class CrossValidationResult:
    available: bool
    reason: str | None
    dimension: CrossGroupDimension
    composite_fingerprint: str
    dataset_sample_ids: tuple[str, ...]
    folds: tuple[CrossValidationFoldResult, ...]
    diagnostics: DiagnosticReport | None
    report_path: Path
    report_fingerprint: str
    protocol_fingerprint: str
    kind: str = CROSS_VALIDATION_REPORT_KIND
    schema_version: int = CROSS_VALIDATION_REPORT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_cross_validation_result(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "available": self.available,
            "reason": self.reason,
            "dimension": self.dimension,
            "composite_fingerprint": self.composite_fingerprint,
            "dataset_sample_ids": list(self.dataset_sample_ids),
            "folds": [fold.to_dict() for fold in self.folds],
            "diagnostics": self.diagnostics.to_dict()
            if self.diagnostics is not None
            else None,
            "protocol_fingerprint": self.protocol_fingerprint,
            "report_fingerprint": self.report_fingerprint,
        }

    @classmethod
    def from_dict(cls, raw: object, *, report_path: Path) -> CrossValidationResult:
        data = _expect_mapping(
            raw,
            {
                "kind",
                "schema_version",
                "available",
                "reason",
                "dimension",
                "composite_fingerprint",
                "dataset_sample_ids",
                "folds",
                "diagnostics",
                "protocol_fingerprint",
                "report_fingerprint",
            },
            "cross-validation report",
        )
        if (
            data["kind"] != CROSS_VALIDATION_REPORT_KIND
            or data["schema_version"] != CROSS_VALIDATION_REPORT_SCHEMA_VERSION
        ):
            raise ValueError("cross-validation report kind/schema versionが不正です")
        if type(data["available"]) is not bool:
            raise ValueError("cross-validation report.availableはboolが必要です")
        reason_raw = data["reason"]
        if reason_raw is not None and (type(reason_raw) is not str or not reason_raw):
            raise ValueError("cross-validation report.reasonが不正です")
        protocol_raw = data["protocol_fingerprint"]
        if type(protocol_raw) is not str:
            raise ValueError("cross-validation report.protocol_fingerprintが不正です")
        folds_raw = _require_list(data["folds"], "cross-validation report.folds")
        diagnostics_raw = data["diagnostics"]
        return cls(
            available=data["available"],
            reason=reason_raw,
            dimension=_cross_dimension(
                data["dimension"], "cross-validation report.dimension"
            ),
            composite_fingerprint=_string(
                data["composite_fingerprint"],
                "cross-validation report.composite_fingerprint",
            ),
            dataset_sample_ids=_string_tuple(
                data["dataset_sample_ids"],
                "cross-validation report.dataset_sample_ids",
            ),
            folds=tuple(
                CrossValidationFoldResult.from_dict(item) for item in folds_raw
            ),
            diagnostics=(
                DiagnosticReport.from_dict(diagnostics_raw)
                if diagnostics_raw is not None
                else None
            ),
            report_path=report_path,
            report_fingerprint=_string(
                data["report_fingerprint"],
                "cross-validation report.report_fingerprint",
            ),
            protocol_fingerprint=protocol_raw,
        )

    @classmethod
    def load(cls, path: Path) -> CrossValidationResult:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(
                f"cross-validation reportを読み込めません: {path}: {error}"
            ) from error
        return cls.from_dict(raw, report_path=path)


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


def _string(raw: object, context: str) -> str:
    if type(raw) is not str or not raw:
        raise ValueError(f"{context}は空でないstringが必要です")
    return raw


def _string_tuple(raw: object, context: str) -> tuple[str, ...]:
    values = _require_list(raw, context)
    if any(type(item) is not str or not item for item in values):
        raise ValueError(f"{context}は空でないstring arrayが必要です")
    return tuple(str(item) for item in values)


def _finite_float(raw: object, context: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{context}は有限numberが必要です")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"{context}は有限numberが必要です")
    return value


def _integer(raw: object, context: str) -> int:
    if type(raw) is not int:
        raise ValueError(f"{context}はintegerが必要です")
    return raw


def _cross_dimension(raw: object, context: str) -> CrossGroupDimension:
    if raw == "machine":
        return "machine"
    if raw == "paste_lot":
        return "paste_lot"
    if raw == "nozzle":
        return "nozzle"
    raise ValueError(f"{context}が不正です")


def _regression_metrics_from_dict(raw: object, context: str) -> RegressionMetrics:
    float_keys = {
        "gaussian_nll",
        "mae_ul",
        "rmse_ul",
        "normalized_error_mean",
        "normalized_error_std",
        "normalized_error_score",
        "median_absolute_relative_error",
        "p95_absolute_relative_error",
        "one_std_coverage",
        "mean_prediction_std_ul",
    }
    integer_keys = {"invalid_prediction_count", "sample_count"}
    data = _expect_mapping(raw, float_keys | integer_keys, context)
    return RegressionMetrics(
        gaussian_nll=_finite_float(data["gaussian_nll"], f"{context}.gaussian_nll"),
        mae_ul=_finite_float(data["mae_ul"], f"{context}.mae_ul"),
        rmse_ul=_finite_float(data["rmse_ul"], f"{context}.rmse_ul"),
        normalized_error_mean=_finite_float(
            data["normalized_error_mean"], f"{context}.normalized_error_mean"
        ),
        normalized_error_std=_finite_float(
            data["normalized_error_std"], f"{context}.normalized_error_std"
        ),
        normalized_error_score=_finite_float(
            data["normalized_error_score"], f"{context}.normalized_error_score"
        ),
        median_absolute_relative_error=_finite_float(
            data["median_absolute_relative_error"],
            f"{context}.median_absolute_relative_error",
        ),
        p95_absolute_relative_error=_finite_float(
            data["p95_absolute_relative_error"],
            f"{context}.p95_absolute_relative_error",
        ),
        one_std_coverage=_finite_float(
            data["one_std_coverage"], f"{context}.one_std_coverage"
        ),
        mean_prediction_std_ul=_finite_float(
            data["mean_prediction_std_ul"], f"{context}.mean_prediction_std_ul"
        ),
        invalid_prediction_count=_integer(
            data["invalid_prediction_count"], f"{context}.invalid_prediction_count"
        ),
        sample_count=_integer(data["sample_count"], f"{context}.sample_count"),
    )


def _prediction_from_dict(raw: object) -> Prediction:
    data = _expect_mapping(
        raw, {"sample_id", "mean_volume_ul", "std_volume_ul"}, "fold prediction"
    )
    return Prediction(
        sample_id=_string(data["sample_id"], "fold prediction.sample_id"),
        mean_volume_ul=_finite_float(
            data["mean_volume_ul"], "fold prediction.mean_volume_ul"
        ),
        std_volume_ul=_finite_float(
            data["std_volume_ul"], "fold prediction.std_volume_ul"
        ),
    )


def _validate_sha256(value: str, context: str) -> None:
    if (
        type(value) is not str
        or not value.startswith("sha256:")
        or len(value) != 71
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError(f"{context}はsha256 fingerprintが必要です")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _canonical_fingerprint(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _cross_validation_evidence(
    *,
    available: bool,
    reason: str | None,
    dimension: CrossGroupDimension,
    composite_fingerprint: str,
    dataset_sample_ids: tuple[str, ...],
    folds: tuple[CrossValidationFoldResult, ...],
    diagnostics: DiagnosticReport | None,
    protocol_fingerprint: str,
) -> dict[str, object]:
    return {
        "kind": CROSS_VALIDATION_REPORT_KIND,
        "schema_version": CROSS_VALIDATION_REPORT_SCHEMA_VERSION,
        "available": available,
        "reason": reason,
        "dimension": dimension,
        "composite_fingerprint": composite_fingerprint,
        "dataset_sample_ids": list(dataset_sample_ids),
        "folds": [fold.evidence_dict() for fold in folds],
        "diagnostics": diagnostics.to_dict() if diagnostics is not None else None,
        "protocol_fingerprint": protocol_fingerprint,
    }


def _validate_cross_validation_result(result: CrossValidationResult) -> None:
    if (
        result.kind != CROSS_VALIDATION_REPORT_KIND
        or result.schema_version != CROSS_VALIDATION_REPORT_SCHEMA_VERSION
    ):
        raise ValueError("cross-validation report kind/schema versionが不正です")
    if result.dimension not in ("machine", "paste_lot", "nozzle"):
        raise ValueError("cross-validation report dimensionが不正です")
    _validate_sha256(result.composite_fingerprint, "composite_fingerprint")
    _validate_sha256(result.report_fingerprint, "report_fingerprint")
    _validate_sha256(result.protocol_fingerprint, "protocol_fingerprint")
    if not result.dataset_sample_ids:
        raise ValueError("cross-validation reportにはdataset sampleが必要です")
    if tuple(sorted(result.dataset_sample_ids)) != result.dataset_sample_ids:
        raise ValueError("dataset sample IDはsort済みである必要があります")
    if len(set(result.dataset_sample_ids)) != len(result.dataset_sample_ids):
        raise ValueError("dataset sample IDが重複しています")
    if result.available:
        if result.reason is not None or not result.folds or result.diagnostics is None:
            raise ValueError(
                "available reportにはfold/diagnosticsとreason=nullが必要です"
            )
        fold_ids = tuple(fold.fold_id for fold in result.folds)
        if len(set(fold_ids)) != len(fold_ids):
            raise ValueError("cross-validation fold IDが重複しています")
        if tuple(sorted(fold_ids)) != fold_ids:
            raise ValueError("cross-validation foldはfold ID順が必要です")
        held_out_groups = tuple(fold.held_out_group for fold in result.folds)
        if len(set(held_out_groups)) != len(held_out_groups):
            raise ValueError("cross-validation held-out groupが重複しています")
        held_out_ids = tuple(
            sample_id for fold in result.folds for sample_id in fold.held_out_sample_ids
        )
        if len(set(held_out_ids)) != len(held_out_ids):
            raise ValueError("cross-validation fold間でheld-out sampleが重複しています")
        if set(held_out_ids) != set(result.dataset_sample_ids):
            raise ValueError(
                "cross-validation foldがdataset sample全体をcoverしていません"
            )
        if any(fold.dimension != result.dimension for fold in result.folds):
            raise ValueError("cross-validation fold dimensionがreportと一致しません")
        if result.diagnostics.sample_ids != result.dataset_sample_ids:
            raise ValueError(
                "cross-validation diagnosticsがdataset sampleをcoverしていません"
            )
    else:
        if not result.reason or result.folds or result.diagnostics is not None:
            raise ValueError(
                "unavailable reportにはreason、empty folds、diagnostics=nullが必要です"
            )
    expected = _canonical_fingerprint(
        _cross_validation_evidence(
            available=result.available,
            reason=result.reason,
            dimension=result.dimension,
            composite_fingerprint=result.composite_fingerprint,
            dataset_sample_ids=result.dataset_sample_ids,
            folds=result.folds,
            diagnostics=result.diagnostics,
            protocol_fingerprint=result.protocol_fingerprint,
        )
    )
    if result.report_fingerprint != expected:
        raise ValueError("cross-validation report fingerprintが内容と一致しません")


def _run_fold(
    config: CrossValidationConfig,
    fold: CrossGroupFold,
    logger_factory: CrossValidationLoggerFactory,
) -> tuple[TrainResult, EvaluationResult]:
    fold_directory = config.output_directory / fold.fold_id
    training_directory = fold_directory / "training"
    evaluation_directory = fold_directory / "evaluation"
    fold_training_config = replace(
        config.training,
        data=replace(config.training.data, split_manifest=None),
        checkpoint=replace(
            config.training.checkpoint,
            directory=training_directory,
            resume_checkpoint=None,
        ),
    )
    training_result = train(
        fold_training_config,
        logger_factory.create(phase="train", fold=fold),
        split_override=fold.split,
    )
    split_path = training_directory / "split.json"
    evaluation_config = EvaluateConfig(
        data=replace(config.training.data, split_manifest=split_path),
        weights=training_result.weights_path,
        output_directory=evaluation_directory,
        split="test",
        allow_frozen_test=True,
        device=config.evaluation_device,
        compile_enabled=config.evaluation_compile_enabled,
        compile_backend=config.training.trainer.compile_backend,
        compile_mode=config.training.trainer.compile_mode,
        repository_root=config.training.repository_root,
    )
    evaluation_result = evaluate(
        evaluation_config,
        logger_factory.create(phase="evaluate", fold=fold),
    )
    expected_ids = set(fold.split.test_sample_ids)
    if {
        prediction.sample_id for prediction in evaluation_result.predictions
    } != expected_ids:
        raise ValueError(
            f"fold {fold.fold_id}のprediction sample集合がheld-out splitと一致しません"
        )
    return training_result, evaluation_result


def run_cross_validation(
    config: CrossValidationConfig,
    logger_factory: CrossValidationLoggerFactory,
) -> CrossValidationResult:
    """Train and evaluate one independent model per machine/lot/nozzle fold."""

    expected_protocol = training_protocol_fingerprint(config.training)
    if (
        config.protocol_fingerprint is not None
        and config.protocol_fingerprint != expected_protocol
    ):
        raise ValueError(
            "cross-validation protocol_fingerprintがtraining configと不一致です"
        )
    protocol_fingerprint = expected_protocol
    if config.output_directory.exists() and any(config.output_directory.iterdir()):
        raise FileExistsError(
            f"cross-validation output directoryは空である必要があります: "
            f"{config.output_directory}"
        )
    composite = resolve_dataset_inputs(
        manifest=config.training.data.manifest,
        roots=config.training.data.roots,
    )
    samples = build_sample_index(composite)
    plan = build_cross_group_plan(
        samples,
        composite.composite_fingerprint,
        config.dimension,
        seed=config.seed,
    )
    config.output_directory.mkdir(parents=True, exist_ok=True)
    report_path = config.output_directory / "cross-validation-report.json"
    dataset_sample_ids = tuple(sorted(sample.sample_id for sample in samples))
    if not plan.available:
        reason = (
            plan.reason
            or f"{config.dimension} leave-one-group-out evaluation is unavailable"
        )
        evidence = _cross_validation_evidence(
            available=False,
            reason=reason,
            dimension=config.dimension,
            composite_fingerprint=composite.composite_fingerprint,
            dataset_sample_ids=dataset_sample_ids,
            folds=(),
            diagnostics=None,
            protocol_fingerprint=protocol_fingerprint,
        )
        result = CrossValidationResult(
            available=False,
            reason=reason,
            dimension=config.dimension,
            composite_fingerprint=composite.composite_fingerprint,
            dataset_sample_ids=dataset_sample_ids,
            folds=(),
            diagnostics=None,
            report_path=report_path,
            report_fingerprint=_canonical_fingerprint(evidence),
            protocol_fingerprint=protocol_fingerprint,
        )
        write_json_artifact(report_path, result.to_dict())
        return result
    fold_results: list[CrossValidationFoldResult] = []
    all_predictions: list[Prediction] = []
    by_sample_id = {sample.sample_id: sample for sample in samples}
    evaluated_ids: set[str] = set()
    for fold in plan.folds:
        training_result, evaluation_result = _run_fold(config, fold, logger_factory)
        if evaluation_result.split_fingerprint != fold.split.split_fingerprint:
            raise ValueError(
                f"fold {fold.fold_id}のevaluation split fingerprintが不一致です"
            )
        predictions = tuple(
            sorted(evaluation_result.predictions, key=lambda item: item.sample_id)
        )
        overlap = evaluated_ids & {prediction.sample_id for prediction in predictions}
        if overlap:
            raise ValueError(
                f"cross-validation fold間でheld-out sampleが重複しています: {sorted(overlap)}"
            )
        evaluated_ids.update(prediction.sample_id for prediction in predictions)
        all_predictions.extend(predictions)
        split_path = config.output_directory / fold.fold_id / "training" / "split.json"
        weights_sha256 = _sha256_file(training_result.weights_path)
        if weights_sha256 != evaluation_result.weights_sha256:
            raise ValueError(
                f"fold {fold.fold_id}のweights hashがevaluation evidenceと一致しません"
            )
        diagnostic_report_sha256 = _sha256_file(
            evaluation_result.diagnostic_report_path
        )
        if diagnostic_report_sha256 != evaluation_result.diagnostic_report_sha256:
            raise ValueError(
                f"fold {fold.fold_id}のdiagnostic report hashがevaluation evidenceと一致しません"
            )
        fold_results.append(
            CrossValidationFoldResult(
                fold_id=fold.fold_id,
                dimension=fold.dimension,
                held_out_group=fold.held_out_group,
                training_run_id=training_result.run_id,
                evaluation_run_id=evaluation_result.run_id,
                weights_path=training_result.weights_path,
                split_path=split_path,
                evaluation_report_path=evaluation_result.report_path,
                diagnostic_report_path=evaluation_result.diagnostic_report_path,
                weights_sha256=weights_sha256,
                split_sha256=_sha256_file(split_path),
                evaluation_report_sha256=_sha256_file(evaluation_result.report_path),
                diagnostic_report_sha256=diagnostic_report_sha256,
                split_fingerprint=fold.split.split_fingerprint,
                held_out_sample_ids=tuple(sorted(fold.split.test_sample_ids)),
                training_best_validation_metrics=training_result.best_validation_metrics,
                held_out_metrics=evaluation_result.metrics,
                predictions=predictions,
                diagnostics=evaluation_result.diagnostics,
            )
        )
    evaluated_samples = tuple(
        by_sample_id[sample_id] for sample_id in sorted(evaluated_ids)
    )
    diagnostics = build_diagnostic_report(evaluated_samples, all_predictions)
    sorted_folds = tuple(sorted(fold_results, key=lambda item: item.fold_id))
    evidence = _cross_validation_evidence(
        available=True,
        reason=None,
        dimension=config.dimension,
        composite_fingerprint=composite.composite_fingerprint,
        dataset_sample_ids=dataset_sample_ids,
        folds=sorted_folds,
        diagnostics=diagnostics,
        protocol_fingerprint=protocol_fingerprint,
    )
    result = CrossValidationResult(
        available=True,
        reason=None,
        dimension=config.dimension,
        composite_fingerprint=composite.composite_fingerprint,
        dataset_sample_ids=dataset_sample_ids,
        folds=sorted_folds,
        diagnostics=diagnostics,
        report_path=report_path,
        report_fingerprint=_canonical_fingerprint(evidence),
        protocol_fingerprint=protocol_fingerprint,
    )
    write_json_artifact(report_path, result.to_dict())
    return result


def load_cross_validation_result(path: Path) -> CrossValidationResult:
    """Persisted cross-validation reportをschema/hash検証付きで読む."""

    return CrossValidationResult.load(path)


__all__ = [
    "CROSS_VALIDATION_REPORT_KIND",
    "CROSS_VALIDATION_REPORT_SCHEMA_VERSION",
    "CrossValidationConfig",
    "CrossValidationFoldResult",
    "CrossValidationLoggerFactory",
    "CrossValidationResult",
    "MLflowCrossValidationLoggerFactory",
    "load_cross_validation_result",
    "run_cross_validation",
]
