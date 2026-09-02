"""ONNX export、edge候補評価、promotion、active model切替.

公開APIは ``weights.pt`` からproduction packageまで一方向に進む。候補選択へ
frozen test結果を渡す入口は設けず、候補固定後の型だけがtest評価とpromotionを許す。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import resource
import shutil
import statistics
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, TypeGuard, cast, override

import attrs
import numpy as np

from .compile_parity import (
    CompileParityBatch,
    CompileParityConfig,
    CompileParityReport,
    compile_parity_report_from_dict,
    run_compile_parity,
    validate_compile_parity_report,
)
from .cross_validation import (
    CrossValidationResult,
    load_cross_validation_result,
)
from .formal_artifact import (
    FormalArtifactAttestation,
    verify_formal_artifact,
)
from .training import FormalTrainingWeights

WEIGHT_SCHEMA_VERSION = 1
EVALUATION_SCHEMA_VERSION = 2
BENCHMARK_SCHEMA_VERSION = 1
MODEL_PACKAGE_SCHEMA_VERSION = 2
ACTIVE_MODEL_POINTER_SCHEMA_VERSION = 1
CROSS_VALIDATION_PROMOTION_EVIDENCE_SCHEMA_VERSION = 1
EXPORT_PARITY_REPORT_SCHEMA_VERSION = 1
DEFAULT_OPSET_VERSION = 18

CROSS_VALIDATION_PROMOTION_EVIDENCE_KIND = (
    "pcbasm-paste-volume-cross-validation-promotion-evidence"
)
EXPORT_PARITY_REPORT_KIND = "pcbasm-paste-volume-export-parity"

PRIMARY_ACCURACY_MAX = 0.10
FP32_RELATIVE_TOLERANCE = 0.001
FP32_ABSOLUTE_TOLERANCE_UL = 0.000001
PADDING_DEGRADATION_MAX = 0.01
INT8_ACCURACY_DEGRADATION_MAX = 0.01
INT8_COVERAGE_DELTA_MAX = 0.03
PI_P95_LATENCY_MS_MAX = 1000.0
THREE_SAMPLE_ACCEPTANCE_MIN = 0.90
BENCHMARK_WARMUP_ITERATIONS = 10
BENCHMARK_MEASURED_ITERATIONS = 100
_PADDING_CONDITIONS = (
    ("padding-10-top", 0.10, "top"),
    ("padding-25-right", 0.25, "right"),
    ("padding-50-bottom", 0.50, "bottom"),
)
_REQUIRED_PADDING_CONDITION_LABELS = frozenset(
    label for label, _, _ in _PADDING_CONDITIONS
)
_MODULE_MONOTONIC_ORIGIN_NS = time.monotonic_ns()
_ALLOWED_ONNX_OPERATORS = frozenset(
    {
        "Add",
        "Cast",
        "Clip",
        "Concat",
        "Constant",
        "Conv",
        "DequantizeLinear",
        "Div",
        "Exp",
        "Flatten",
        "Gather",
        "Gemm",
        "GlobalAveragePool",
        "Greater",
        "Identity",
        "InstanceNormalization",
        "Log",
        "MatMul",
        "Max",
        "Mul",
        "Pow",
        "QuantizeLinear",
        "ReduceMax",
        "ReduceMean",
        "ReduceMin",
        "Relu",
        "Reshape",
        "Shape",
        "Sigmoid",
        "Slice",
        "Softplus",
        "Sqrt",
        "Squeeze",
        "Sub",
        "Transpose",
        "Unsqueeze",
        "Where",
    }
)

type ModelFormat = Literal["onnx-fp32", "onnx-int8-qdq"]
type OnnxArtifactRole = Literal["export-fp32", "optimized-fp32", "int8-qdq"]
type BenchmarkCategory = Literal["small", "medium", "large", "portrait", "landscape"]
type ExportParityShapeCase = Literal["minimum", "maximum-area", "portrait", "landscape"]

_BENCHMARK_CATEGORIES: tuple[BenchmarkCategory, ...] = (
    "small",
    "medium",
    "large",
    "portrait",
    "landscape",
)
_EXPORT_PARITY_SHAPES: tuple[tuple[ExportParityShapeCase, tuple[int, int]], ...] = (
    ("minimum", (32, 32)),
    ("maximum-area", (512, 512)),
    ("portrait", (1024, 256)),
    ("landscape", (256, 1024)),
)


@attrs.frozen
class ArtifactLineage:
    """trainingからpromotionまで全artifactへ伝播するlineage."""

    source_run_id: str
    source_checkpoint_sha256: str
    dataset_fingerprint: str
    split_fingerprint: str
    training_protocol_fingerprint: str
    source_checkpoint_role: Literal["best"] = "best"
    parent_run_id: str | None = None
    parent_checkpoint_id: str | None = None

    def __attrs_post_init__(self) -> None:
        for name in (
            "source_run_id",
            "source_checkpoint_sha256",
            "dataset_fingerprint",
            "split_fingerprint",
        ):
            if not getattr(self, name):
                raise ValueError(f"{name}を空にできません")
        if (self.parent_run_id is None) != (self.parent_checkpoint_id is None):
            raise ValueError("fine-tune parent run/checkpointは両方指定してください")
        if self.source_checkpoint_role != "best":
            raise ValueError("export source checkpoint roleはbestだけを許可します")
        for name in (
            "source_checkpoint_sha256",
            "dataset_fingerprint",
            "split_fingerprint",
        ):
            if not _is_prefixed_sha256(getattr(self, name)):
                raise ValueError(f"{name}が不正です")
        if not _is_prefixed_sha256(self.training_protocol_fingerprint):
            raise ValueError("training_protocol_fingerprintが不正です")

    def to_dict(self) -> dict[str, str | None]:
        return attrs.asdict(self)


@attrs.frozen
class OnnxExportResult:
    weights_path: Path
    model_path: Path
    model_artifact_sha256: str
    opset_version: int
    node_types: tuple[str, ...]
    dynamic_shapes: tuple[tuple[int, int], ...]
    lineage: ArtifactLineage
    model_config: Mapping[str, Any]
    preprocess_schema: Mapping[str, Any]
    uncertainty_log_variance_offset: float

    def to_dict(self) -> dict[str, object]:
        return {
            "weights_path": str(self.weights_path),
            "model_path": str(self.model_path),
            "model_artifact_sha256": self.model_artifact_sha256,
            "opset_version": self.opset_version,
            "node_types": list(self.node_types),
            "dynamic_shapes": [list(shape) for shape in self.dynamic_shapes],
            "lineage": self.lineage.to_dict(),
            "model_config": dict(self.model_config),
            "preprocess_schema": dict(self.preprocess_schema),
            "uncertainty_log_variance_offset": self.uncertainty_log_variance_offset,
        }


@attrs.frozen
class OnnxParityResult:
    model_artifact_sha256: str
    shapes: tuple[tuple[int, int], ...]
    maximum_mean_absolute_error_ul: float
    maximum_log_variance_absolute_error: float
    passed: bool

    def to_dict(self) -> dict[str, object]:
        return attrs.asdict(self)


@attrs.frozen
class ExportParitySampleResult:
    """One persisted validation sample's eager-vs-FP32 ONNX comparison."""

    sample_id: str
    mean_absolute_error_ul: float
    mean_tolerance_ul: float
    log_variance_absolute_error: float
    log_variance_tolerance: float
    passed: bool

    def __attrs_post_init__(self) -> None:
        if not self.sample_id:
            raise ValueError("export parity sample_idを空にできません")
        if type(self.passed) is not bool:
            raise ValueError("export parity sample passedはboolが必要です")
        for name in (
            "mean_absolute_error_ul",
            "mean_tolerance_ul",
            "log_variance_absolute_error",
            "log_variance_tolerance",
        ):
            value = getattr(self, name)
            if not _is_nonnegative_finite(value):
                raise ValueError(f"export parity {name}が不正です")
        if self.mean_tolerance_ul <= 0 or self.log_variance_tolerance <= 0:
            raise ValueError("export parity toleranceは正の有限値が必要です")
        expected = (
            self.mean_absolute_error_ul <= self.mean_tolerance_ul
            and self.log_variance_absolute_error <= self.log_variance_tolerance
        )
        if self.passed is not expected:
            raise ValueError("export parity sample gate statusが数値と不一致です")

    def to_dict(self) -> dict[str, object]:
        return attrs.asdict(self)


@attrs.frozen
class ExportParityShapeResult:
    """One required dynamic-shape eager-vs-FP32 ONNX comparison."""

    case_name: ExportParityShapeCase
    image_height: int
    image_width: int
    mean_absolute_error_ul: float
    mean_tolerance_ul: float
    log_variance_absolute_error: float
    log_variance_tolerance: float
    passed: bool

    def __attrs_post_init__(self) -> None:
        expected_shapes = dict(_EXPORT_PARITY_SHAPES)
        if self.case_name not in expected_shapes:
            raise ValueError("export parity dynamic shape caseが不正です")
        if (self.image_height, self.image_width) != expected_shapes[self.case_name]:
            raise ValueError("export parity dynamic shape寸法がcaseと不一致です")
        if type(self.passed) is not bool:
            raise ValueError("export parity dynamic shape passedはboolが必要です")
        for name in (
            "mean_absolute_error_ul",
            "mean_tolerance_ul",
            "log_variance_absolute_error",
            "log_variance_tolerance",
        ):
            if not _is_nonnegative_finite(getattr(self, name)):
                raise ValueError(f"export parity dynamic shape {name}が不正です")
        if self.mean_tolerance_ul <= 0 or self.log_variance_tolerance <= 0:
            raise ValueError(
                "export parity dynamic shape toleranceは正の有限値が必要です"
            )
        expected = (
            self.mean_absolute_error_ul <= self.mean_tolerance_ul
            and self.log_variance_absolute_error <= self.log_variance_tolerance
        )
        if self.passed is not expected:
            raise ValueError(
                "export parity dynamic shape gate statusが数値と不一致です"
            )

    def to_dict(self) -> dict[str, object]:
        return attrs.asdict(self)


@attrs.frozen
class ExportParityReport:
    """Validation samples and required dynamic shapes bound to export
    parity."""

    kind: str
    schema_version: int
    weights_sha256: str
    fp32_model_artifact_sha256: str
    training_protocol_fingerprint: str
    dataset_fingerprint: str
    split_fingerprint: str
    split_name: Literal["validation"]
    sample_ids: tuple[str, ...]
    training_sample_ids: tuple[str, ...]
    sample_results: tuple[ExportParitySampleResult, ...]
    shape_results: tuple[ExportParityShapeResult, ...]
    maximum_mean_absolute_error_ul: float
    maximum_log_variance_absolute_error: float
    success: bool
    content_sha256: str

    def __attrs_post_init__(self) -> None:
        if self.kind != EXPORT_PARITY_REPORT_KIND:
            raise ValueError("export parity report kindが不正です")
        if self.schema_version != EXPORT_PARITY_REPORT_SCHEMA_VERSION:
            raise ValueError("export parity report schemaが不正です")
        if not _is_prefixed_sha256(self.weights_sha256):
            raise ValueError("export parity weights_sha256が不正です")
        if not _is_sha256(self.fp32_model_artifact_sha256):
            raise ValueError("export parity FP32 model hashが不正です")
        for name in (
            "training_protocol_fingerprint",
            "dataset_fingerprint",
            "split_fingerprint",
        ):
            if not _is_prefixed_sha256(getattr(self, name)):
                raise ValueError(f"export parity {name}が不正です")
        if self.split_name != "validation":
            raise ValueError("export parityはvalidation assignmentだけを許可します")
        if not self.sample_ids or tuple(sorted(self.sample_ids)) != self.sample_ids:
            raise ValueError("export parity sample IDsは空でないsort済み集合が必要です")
        if len(set(self.sample_ids)) != len(self.sample_ids):
            raise ValueError("export parity sample IDsが重複しています")
        if (
            not self.training_sample_ids
            or tuple(sorted(self.training_sample_ids)) != self.training_sample_ids
            or len(set(self.training_sample_ids)) != len(self.training_sample_ids)
        ):
            raise ValueError(
                "export parity training sample IDsは空でないsort済み集合が必要です"
            )
        if tuple(item.sample_id for item in self.sample_results) != self.sample_ids:
            raise ValueError("export parity sample resultがassignmentと一致しません")
        expected_shapes = tuple(_EXPORT_PARITY_SHAPES)
        actual_shapes = tuple(
            (item.case_name, (item.image_height, item.image_width))
            for item in self.shape_results
        )
        if actual_shapes != expected_shapes:
            raise ValueError(
                "export parityにはminimum/maximum-area/portrait/landscapeが必要です"
            )
        all_results = (*self.sample_results, *self.shape_results)
        expected_mean = max(item.mean_absolute_error_ul for item in all_results)
        expected_log_variance = max(
            item.log_variance_absolute_error for item in all_results
        )
        if (
            self.maximum_mean_absolute_error_ul != expected_mean
            or self.maximum_log_variance_absolute_error != expected_log_variance
        ):
            raise ValueError("export parity aggregate errorがsample結果と不一致です")
        if type(self.success) is not bool or self.success is not all(
            item.passed for item in all_results
        ):
            raise ValueError("export parity successがsample/shape結果と不一致です")

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "weights_sha256": self.weights_sha256,
            "fp32_model_artifact_sha256": self.fp32_model_artifact_sha256,
            "training_protocol_fingerprint": self.training_protocol_fingerprint,
            "dataset_fingerprint": self.dataset_fingerprint,
            "split_fingerprint": self.split_fingerprint,
            "split_name": self.split_name,
            "sample_ids": list(self.sample_ids),
            "training_sample_ids": list(self.training_sample_ids),
            "sample_results": [item.to_dict() for item in self.sample_results],
            "shape_results": [item.to_dict() for item in self.shape_results],
            "maximum_mean_absolute_error_ul": self.maximum_mean_absolute_error_ul,
            "maximum_log_variance_absolute_error": (
                self.maximum_log_variance_absolute_error
            ),
            "success": self.success,
            "content_sha256": self.content_sha256,
        }


@attrs.frozen
class OptimizationResult:
    source_model_path: Path
    source_model_artifact_sha256: str
    optimized_fp32_path: Path
    optimized_fp32_artifact_sha256: str
    int8_path: Path
    int8_artifact_sha256: str
    calibration_sample_ids: tuple[str, ...]
    calibration_session_ids: tuple[str, ...]
    calibration_sample_index_fingerprint: str
    split_manifest_path: Path
    lineage: ArtifactLineage

    def to_dict(self) -> dict[str, object]:
        return {
            "source_model_path": str(self.source_model_path),
            "source_model_artifact_sha256": self.source_model_artifact_sha256,
            "optimized_fp32_path": str(self.optimized_fp32_path),
            "optimized_fp32_artifact_sha256": self.optimized_fp32_artifact_sha256,
            "int8_path": str(self.int8_path),
            "int8_artifact_sha256": self.int8_artifact_sha256,
            "calibration_sample_ids": list(self.calibration_sample_ids),
            "calibration_session_ids": list(self.calibration_session_ids),
            "calibration_sample_index_fingerprint": (
                self.calibration_sample_index_fingerprint
            ),
            "split_manifest_path": str(self.split_manifest_path),
            "lineage": self.lineage.to_dict(),
        }


@attrs.frozen
class CandidatePrediction:
    """同一sampleに対する候補とFP32 referenceの出力."""

    sample_id: str
    target_volume_ul: float
    mean_volume_ul: float
    log_variance_volume_ul2: float
    reference_mean_volume_ul: float
    reference_log_variance_volume_ul2: float
    sequence_group: str
    sequence_index: int
    padded_mean_volume_ul: Mapping[str, float] = attrs.field(factory=dict)

    def __attrs_post_init__(self) -> None:
        if not self.sample_id:
            raise ValueError("sample_idを空にできません")
        if not self.sequence_group:
            raise ValueError("sequence_groupを空にできません")
        if type(self.sequence_index) is not int or self.sequence_index < 0:
            raise ValueError("sequence_indexは0以上の整数が必要です")
        if not _is_positive_finite(self.target_volume_ul):
            raise ValueError("target_volume_ulは正の有限値が必要です")


@attrs.frozen
class CandidateEvaluation:
    """Validationまたは固定後testの数値gate report."""

    phase: Literal["validation", "frozen_test"]
    candidate_id: str
    model_format: ModelFormat
    model_artifact_sha256: str
    fp32_reference_artifact_sha256: str
    training_dataset_fingerprint: str
    training_split_fingerprint: str
    training_protocol_fingerprint: str
    dataset_fingerprint: str
    split_fingerprint: str
    evaluated_sample_ids: tuple[str, ...]
    training_sample_ids: tuple[str, ...]
    source_run_id: str
    source_checkpoint_sha256: str
    source_checkpoint_role: Literal["best"]
    parent_run_id: str | None
    parent_checkpoint_id: str | None
    sample_count: int
    primary_accuracy_score: float
    coverage_68: float
    fp32_reference_accuracy_score: float
    fp32_reference_coverage_68: float
    maximum_mean_parity_error_ul: float
    maximum_mean_parity_tolerance_ul: float
    maximum_mean_parity_ratio: float
    maximum_log_variance_parity_error: float
    maximum_log_variance_parity_tolerance: float
    maximum_log_variance_parity_ratio: float
    fp32_parity_passed: bool
    padding_accuracy_degradation: float
    uncertainty_relative_std_threshold: float
    accepted_accuracy_score: float
    accepted_sample_coverage: float
    three_sample_acceptance: float
    gate_passed: bool
    gate_failures: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        value = attrs.asdict(self)
        value["gate_failures"] = list(self.gate_failures)
        value["evaluated_sample_ids"] = list(self.evaluated_sample_ids)
        value["training_sample_ids"] = list(self.training_sample_ids)
        return value


@attrs.frozen
class BenchmarkCategoryResult:
    """1つの代表categoryを独立に10/100回測定した証跡."""

    category: BenchmarkCategory
    sample_id: str
    image_height: int
    image_width: int
    warmup_iterations: int
    measured_iterations: int
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float

    @property
    def gate_passed(self) -> bool:
        return (
            self.category in _BENCHMARK_CATEGORIES
            and bool(self.sample_id)
            and self.image_height > 0
            and self.image_width > 0
            and self.warmup_iterations == BENCHMARK_WARMUP_ITERATIONS
            and self.measured_iterations == BENCHMARK_MEASURED_ITERATIONS
            and all(
                _is_nonnegative_finite(value)
                for value in (
                    self.p50_latency_ms,
                    self.p95_latency_ms,
                    self.p99_latency_ms,
                )
            )
            and self.p95_latency_ms <= PI_P95_LATENCY_MS_MAX
        )

    def to_dict(self) -> dict[str, object]:
        return attrs.asdict(self)


@attrs.frozen
class ModelBenchmarkResult:
    """Production同等preprocessを含むbatch-1 CPU benchmark."""

    schema_version: int
    candidate_id: str
    model_artifact_sha256: str
    source_run_id: str
    source_checkpoint_sha256: str
    source_checkpoint_role: Literal["best"]
    training_dataset_fingerprint: str
    training_split_fingerprint: str
    training_protocol_fingerprint: str
    dataset_fingerprint: str
    split_fingerprint: str
    benchmark_sample_ids: tuple[str, ...]
    parent_run_id: str | None
    parent_checkpoint_id: str | None
    platform_model: str
    is_raspberry_pi_5: bool
    cold_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    peak_rss_bytes: int
    artifact_size_bytes: int
    sample_count: int
    warmup_iterations: int
    measured_iterations: int
    category_results: tuple[BenchmarkCategoryResult, ...]
    cold_start_clock: str
    cold_start_origin: str
    os_id: str
    os_release: str
    python_version: str
    onnxruntime_version: str
    cpu_governor: str
    platform_machine: str
    power_condition: str
    cooling_condition: str

    @property
    def gate_passed(self) -> bool:
        return (
            self.is_raspberry_pi_5
            and self.p95_latency_ms <= PI_P95_LATENCY_MS_MAX
            and type(self.warmup_iterations) is int
            and self.warmup_iterations == BENCHMARK_WARMUP_ITERATIONS
            and type(self.measured_iterations) is int
            and self.measured_iterations == BENCHMARK_MEASURED_ITERATIONS
            and self.sample_count == len(_BENCHMARK_CATEGORIES)
            and tuple(result.category for result in self.category_results)
            == _BENCHMARK_CATEGORIES
            and len({result.sample_id for result in self.category_results})
            == len(_BENCHMARK_CATEGORIES)
            and all(result.gate_passed for result in self.category_results)
            and self.p95_latency_ms
            == max(result.p95_latency_ms for result in self.category_results)
            and self.platform_machine.lower() == "aarch64"
            and all(
                value.strip()
                for value in (
                    self.cold_start_clock,
                    self.cold_start_origin,
                    self.os_id,
                    self.os_release,
                    self.python_version,
                    self.onnxruntime_version,
                    self.cpu_governor,
                    self.power_condition,
                    self.cooling_condition,
                )
            )
        )

    def to_dict(self) -> dict[str, object]:
        value = attrs.asdict(self)
        value["benchmark_sample_ids"] = list(self.benchmark_sample_ids)
        value["category_results"] = [
            result.to_dict() for result in self.category_results
        ]
        return value


@attrs.frozen
class BenchmarkSample:
    """Benchmarkへ渡す代表的なlossless RGB pair."""

    sample_id: str
    category: BenchmarkCategory
    pre_rgb: np.ndarray[Any, np.dtype[np.uint8]] = attrs.field(eq=False)
    post_rgb: np.ndarray[Any, np.dtype[np.uint8]] = attrs.field(eq=False)
    pixel_per_mm: float


@attrs.frozen
class _DatasetSplitPredictions:
    predictions: tuple[CandidatePrediction, ...]
    dataset_fingerprint: str
    split_fingerprint: str
    training_sample_ids: tuple[str, ...]


@attrs.frozen
class TrainingCoverage:
    """自動補正を許可するtraining metadata範囲."""

    pixel_per_mm_min: float
    pixel_per_mm_max: float
    height_min: int
    height_max: int
    width_min: int
    width_max: int

    def __attrs_post_init__(self) -> None:
        for minimum, maximum, name in (
            (self.pixel_per_mm_min, self.pixel_per_mm_max, "pixel_per_mm"),
            (self.height_min, self.height_max, "height"),
            (self.width_min, self.width_max, "width"),
        ):
            if not _is_positive_finite(minimum) or not _is_positive_finite(maximum):
                raise ValueError(f"training coverage {name}は正の有限値が必要です")
            if minimum > maximum:
                raise ValueError(f"training coverage {name}のmin/maxが逆です")

    def to_dict(self) -> dict[str, dict[str, float | int]]:
        return {
            "pixel_per_mm": {
                "min": self.pixel_per_mm_min,
                "max": self.pixel_per_mm_max,
            },
            "height": {"min": self.height_min, "max": self.height_max},
            "width": {"min": self.width_min, "max": self.width_max},
        }


@attrs.frozen
class ModelCandidateValidation:
    """Validation gateとPi benchmarkをartifact hashでbindした候補."""

    evaluation: CandidateEvaluation
    benchmark: ModelBenchmarkResult
    compile_parity: CompileParityReport
    export_parity: ExportParityReport
    model_path: Path
    artifact_size_bytes: int
    dependency_complexity_rank: int

    @property
    def candidate_id(self) -> str:
        return self.evaluation.candidate_id

    @property
    def gate_passed(self) -> bool:
        return (
            self.evaluation.gate_passed
            and self.benchmark.gate_passed
            and self.compile_parity.success
            and self.export_parity.success
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "evaluation": self.evaluation.to_dict(),
            "benchmark": self.benchmark.to_dict(),
            "compile_parity": self.compile_parity.to_dict(),
            "export_parity": self.export_parity.to_dict(),
            "model_path": str(self.model_path),
            "artifact_size_bytes": self.artifact_size_bytes,
            "dependency_complexity_rank": self.dependency_complexity_rank,
            "gate_passed": self.gate_passed,
        }


@attrs.frozen
class SelectedModelCandidate:
    """Validationだけで固定された唯一の候補（frozen test未実行）."""

    candidate: ModelCandidateValidation
    compared_candidate_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate": self.candidate.to_dict(),
            "compared_candidate_ids": list(self.compared_candidate_ids),
        }


@attrs.frozen
class FinalizedModelCandidate:
    """候補固定後にだけ実行できるfrozen testを通過したartifact."""

    selection: SelectedModelCandidate
    frozen_test: CandidateEvaluation

    def to_dict(self) -> dict[str, object]:
        return {
            "selection": self.selection.to_dict(),
            "frozen_test": self.frozen_test.to_dict(),
        }


@attrs.frozen
class CrossValidationPromotionEvidence:
    """One strict cross-validation report bound to its formal summary run."""

    report: CrossValidationResult
    summary_attestation: FormalArtifactAttestation
    kind: str = CROSS_VALIDATION_PROMOTION_EVIDENCE_KIND
    schema_version: int = CROSS_VALIDATION_PROMOTION_EVIDENCE_SCHEMA_VERSION

    def __attrs_post_init__(self) -> None:
        if self.kind != CROSS_VALIDATION_PROMOTION_EVIDENCE_KIND:
            raise ValueError("cross-validation promotion evidence kindが不正です")
        if self.schema_version != CROSS_VALIDATION_PROMOTION_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("cross-validation promotion evidence schemaが不正です")
        if not isinstance(self.report, CrossValidationResult):
            raise TypeError("cross-validation promotion reportはtyped objectが必要です")
        if not isinstance(self.summary_attestation, FormalArtifactAttestation):
            raise TypeError(
                "cross-validation promotion attestationはtyped objectが必要です"
            )
        attestation = self.summary_attestation
        if (
            attestation.run_kind != "cross-validation-summary"
            or attestation.output_kind != "file"
            or attestation.output_path != self.report.report_path.resolve()
        ):
            raise ValueError(
                "cross-validation reportとformal summary attestationが一致しません"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "report": self.report.to_dict(),
            "summary_attestation": self.summary_attestation.to_dict(),
        }

    def manifest_summary(self) -> dict[str, object]:
        attestation = self.summary_attestation
        return {
            "dimension": self.report.dimension,
            "report_fingerprint": self.report.report_fingerprint,
            "report_artifact_sha256": attestation.output_fingerprint,
            "summary_run_id": attestation.run_id,
            "tracking_uri_sha256": attestation.tracking_uri_sha256,
            "attestation_sha256": "sha256:"
            + _canonical_json_sha256(attestation.to_dict()),
        }


def load_formal_cross_validation_evidence(
    report_path: Path,
    *,
    tracking_uri: str,
) -> CrossValidationPromotionEvidence:
    """Load one report only after verifying its live formal MLflow summary."""

    persisted_path = Path(report_path).expanduser().resolve(strict=True)
    report = load_cross_validation_result(persisted_path)
    if not report.available or report.reason is not None:
        raise ValueError("releaseにはavailable=trueのcross-validationが必要です")
    attestation = verify_formal_artifact(
        persisted_path,
        tracking_uri=tracking_uri,
        expected_run_kind="cross-validation-summary",
    )
    return CrossValidationPromotionEvidence(
        report=report,
        summary_attestation=attestation,
    )


@attrs.frozen
class ModelPackageResult:
    status: Literal["candidate", "promoted"]
    package_path: Path
    model_id: str
    model_artifact_sha256: str
    manifest_sha256: str
    lineage: ArtifactLineage

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "package_path": str(self.package_path),
            "model_id": self.model_id,
            "model_artifact_sha256": self.model_artifact_sha256,
            "manifest_sha256": self.manifest_sha256,
            "lineage": self.lineage.to_dict(),
        }


@attrs.frozen
class ActiveModelPointer:
    pointer_path: Path
    active_model_path: Path
    previous_model_path: Path | None
    active_model_id: str
    active_model_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": ACTIVE_MODEL_POINTER_SCHEMA_VERSION,
            "pointer_path": str(self.pointer_path),
            "active_model_path": str(self.active_model_path),
            "previous_model_path": (
                None
                if self.previous_model_path is None
                else str(self.previous_model_path)
            ),
            "active_model_id": self.active_model_id,
            "active_model_sha256": self.active_model_sha256,
        }


@attrs.define
class PasteVolumeReleasePipeline:
    """操作順を強制する高水準release pipeline.

    validation候補を固定するまでfrozen test APIへ到達できず、test通過前には
    promotionできない。状態はprocess内のorchestrationだけに使い、artifact自体が
    lineageとchecksumの正典である。
    """

    weights_path: Path
    workspace: Path
    _stage: str = attrs.field(default="weights", init=False)
    _export_result: OnnxExportResult | None = attrs.field(default=None, init=False)
    _optimization_result: OptimizationResult | None = attrs.field(
        default=None, init=False
    )
    _selection: SelectedModelCandidate | None = attrs.field(default=None, init=False)
    _finalized: FinalizedModelCandidate | None = attrs.field(default=None, init=False)

    @property
    def stage(self) -> str:
        return self._stage

    def export(self, **kwargs: Any) -> OnnxExportResult:
        if self._stage != "weights":
            raise RuntimeError(f"exportできないpipeline stageです: {self._stage}")
        result = export_onnx_model(
            self.weights_path,
            self.workspace / "export" / "model.fp32.onnx",
            **kwargs,
        )
        self._export_result = result
        self._stage = "exported"
        return result

    def optimize(
        self,
        *,
        calibration_data: Sequence[Path],
        split_manifest: Path,
        **kwargs: Any,
    ) -> OptimizationResult:
        if self._stage != "exported" or self._export_result is None:
            raise RuntimeError("optimizationはexport完了後だけ実行できます")
        result = optimize_model(
            self._export_result.model_path,
            self.workspace / "optimized",
            calibration_data=calibration_data,
            split_manifest=split_manifest,
            **kwargs,
        )
        self._optimization_result = result
        self._stage = "optimized"
        return result

    def select(
        self, candidates: Sequence[ModelCandidateValidation]
    ) -> SelectedModelCandidate:
        if self._stage != "optimized":
            raise RuntimeError("candidate選択はoptimization完了後だけ実行できます")
        selection = select_model_candidate(candidates)
        self._selection = selection
        self._stage = "selected"
        return selection

    def evaluate_frozen_test(
        self, predictions: Sequence[CandidatePrediction]
    ) -> CandidateEvaluation:
        if self._stage != "selected" or self._selection is None:
            raise RuntimeError("frozen testはvalidation候補固定後だけ実行できます")
        raise ValueError(
            "raw predictionではrelease用frozen testを評価できません。"
            "evaluate_frozen_test_from_datasetを使用してください"
        )

    def evaluate_frozen_test_from_dataset(
        self,
        fp32_reference_model: Path,
        *,
        dataset_paths: Sequence[Path],
        split_manifest: Path,
        allow_external_split: bool = False,
    ) -> CandidateEvaluation:
        if self._stage != "selected" or self._selection is None:
            raise RuntimeError("frozen testはvalidation候補固定後だけ実行できます")
        return evaluate_frozen_test_candidate_from_dataset(
            self._selection,
            fp32_reference_model,
            dataset_paths=dataset_paths,
            split_manifest=split_manifest,
            allow_external_split=allow_external_split,
        )

    def finalize(self, frozen_test: CandidateEvaluation) -> FinalizedModelCandidate:
        if self._stage != "selected" or self._selection is None:
            raise RuntimeError("finalizeはcandidate固定後だけ実行できます")
        finalized = finalize_selected_model_candidate(self._selection, frozen_test)
        self._finalized = finalized
        self._stage = "tested"
        return finalized

    def promote(self, **kwargs: Any) -> ModelPackageResult:
        if self._stage != "tested" or self._finalized is None:
            raise RuntimeError("promotionはfrozen test通過後だけ実行できます")
        result = promote_model_package_from_dataset(self._finalized, **kwargs)
        self._stage = "promoted"
        return result


def benchmark_model_candidate(
    model_path: Path,
    *,
    model_format: ModelFormat,
    preprocess_schema: Mapping[str, Any],
    lineage: ArtifactLineage,
    evaluation_dataset_fingerprint: str,
    evaluation_split_fingerprint: str,
    samples: Sequence[BenchmarkSample],
    power_condition: str,
    cooling_condition: str,
    warmup_iterations: int = BENCHMARK_WARMUP_ITERATIONS,
    measured_iterations: int = BENCHMARK_MEASURED_ITERATIONS,
) -> ModelBenchmarkResult:
    """同じPython前処理とCPU ORTでcold/warm latencyを実測する.

    promotion可能なreportはRaspberry Pi 5上で実行した場合だけ生成される。
    """

    if not power_condition.strip() or not cooling_condition.strip():
        raise ValueError("benchmarkのpower/cooling condition申告が必要です")
    if not evaluation_dataset_fingerprint or not evaluation_split_fingerprint:
        raise ValueError("benchmark evaluation dataset/split fingerprintが必要です")
    sample_ids = tuple(sample.sample_id for sample in samples)
    if any(not sample_id for sample_id in sample_ids):
        raise ValueError("benchmark sample_idを空にできません")
    if len(set(sample_ids)) != len(sample_ids):
        raise ValueError("benchmark sample_idが重複しています")
    by_category = {sample.category: sample for sample in samples}
    if (
        len(by_category) != len(samples)
        or tuple(
            category for category in _BENCHMARK_CATEGORIES if category in by_category
        )
        != _BENCHMARK_CATEGORIES
        or len(samples) != len(_BENCHMARK_CATEGORIES)
    ):
        raise ValueError(
            "benchmarkにはsmall/medium/large/portrait/landscape各1sampleが必要です"
        )
    ordered_samples = tuple(by_category[category] for category in _BENCHMARK_CATEGORIES)
    sample_ids = tuple(sample.sample_id for sample in ordered_samples)
    if (
        type(warmup_iterations) is not int
        or warmup_iterations != BENCHMARK_WARMUP_ITERATIONS
    ):
        raise ValueError(
            f"production benchmark warm-upは{BENCHMARK_WARMUP_ITERATIONS}回が必要です"
        )
    if (
        type(measured_iterations) is not int
        or measured_iterations != BENCHMARK_MEASURED_ITERATIONS
    ):
        raise ValueError(
            f"production benchmark測定は{BENCHMARK_MEASURED_ITERATIONS}回が必要です"
        )
    _validate_canonical_preprocess_schema(preprocess_schema)
    path = Path(model_path).expanduser().resolve(strict=True)
    _validate_candidate_artifact(path, model_format)
    artifact_hash = _sha256_file(path)
    candidate_id = _candidate_id(model_format, artifact_hash)
    metadata = _read_onnx_export_metadata(path)
    embedded_lineage = _lineage_from_mapping(_required_mapping(metadata, "lineage"))
    if embedded_lineage != lineage:
        raise ValueError("benchmark modelのembedded training lineageが不一致です")
    embedded_preprocess = _required_mapping(metadata, "preprocess_schema")
    if _canonical_json(embedded_preprocess) != _canonical_json(preprocess_schema):
        raise ValueError("benchmark modelのpreprocess schemaが不一致です")

    import onnxruntime as ort

    cold_start_ns, cold_start_origin = _process_start_monotonic_ns()
    session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    _run_benchmark_sample(session, ordered_samples[0], preprocess_schema)
    cold_latency_ms = (time.monotonic_ns() - cold_start_ns) / 1_000_000.0
    category_results: list[BenchmarkCategoryResult] = []
    all_latencies: list[float] = []
    for sample in ordered_samples:
        for _ in range(warmup_iterations):
            _run_benchmark_sample(session, sample, preprocess_schema)
        latencies: list[float] = []
        for _ in range(measured_iterations):
            started = time.perf_counter_ns()
            _run_benchmark_sample(session, sample, preprocess_schema)
            latencies.append((time.perf_counter_ns() - started) / 1_000_000.0)
        all_latencies.extend(latencies)
        category_results.append(
            BenchmarkCategoryResult(
                category=sample.category,
                sample_id=sample.sample_id,
                image_height=int(sample.pre_rgb.shape[0]),
                image_width=int(sample.pre_rgb.shape[1]),
                warmup_iterations=warmup_iterations,
                measured_iterations=measured_iterations,
                p50_latency_ms=float(np.percentile(latencies, 50)),
                p95_latency_ms=float(np.percentile(latencies, 95)),
                p99_latency_ms=float(np.percentile(latencies, 99)),
            )
        )

    model_name = _platform_model()
    maximum_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_bytes = (
        int(maximum_rss * 1024) if platform.system() != "Darwin" else int(maximum_rss)
    )
    return ModelBenchmarkResult(
        schema_version=BENCHMARK_SCHEMA_VERSION,
        candidate_id=candidate_id,
        model_artifact_sha256=artifact_hash,
        source_run_id=lineage.source_run_id,
        source_checkpoint_sha256=lineage.source_checkpoint_sha256,
        source_checkpoint_role=lineage.source_checkpoint_role,
        training_dataset_fingerprint=lineage.dataset_fingerprint,
        training_split_fingerprint=lineage.split_fingerprint,
        training_protocol_fingerprint=lineage.training_protocol_fingerprint,
        dataset_fingerprint=evaluation_dataset_fingerprint,
        split_fingerprint=evaluation_split_fingerprint,
        benchmark_sample_ids=sample_ids,
        parent_run_id=lineage.parent_run_id,
        parent_checkpoint_id=lineage.parent_checkpoint_id,
        platform_model=model_name,
        is_raspberry_pi_5="raspberry pi 5" in model_name.lower(),
        cold_latency_ms=cold_latency_ms,
        p50_latency_ms=float(np.percentile(all_latencies, 50)),
        p95_latency_ms=max(result.p95_latency_ms for result in category_results),
        p99_latency_ms=max(result.p99_latency_ms for result in category_results),
        peak_rss_bytes=rss_bytes,
        artifact_size_bytes=path.stat().st_size,
        sample_count=len(ordered_samples),
        warmup_iterations=warmup_iterations,
        measured_iterations=measured_iterations,
        category_results=tuple(category_results),
        cold_start_clock="CLOCK_MONOTONIC",
        cold_start_origin=cold_start_origin,
        os_id=_os_id(),
        os_release=platform.release(),
        python_version=platform.python_version(),
        onnxruntime_version=ort.__version__,
        cpu_governor=_cpu_governor(),
        platform_machine=platform.machine(),
        power_condition=power_condition.strip(),
        cooling_condition=cooling_condition.strip(),
    )


def evaluate_validation_candidate_from_dataset(
    candidate_model: Path,
    fp32_reference_model: Path,
    *,
    model_format: ModelFormat,
    dataset_paths: Sequence[Path],
    split_manifest: Path,
) -> CandidateEvaluation:
    """Persisted validation assignmentを共有preprocess+実ORTで候補評価する."""

    candidate = Path(candidate_model).expanduser().resolve(strict=True)
    reference = Path(fp32_reference_model).expanduser().resolve(strict=True)
    _validate_candidate_reference_artifacts(candidate, reference, model_format)
    lineage = read_onnx_artifact_lineage(candidate)
    reference_lineage = read_onnx_artifact_lineage(reference)
    if reference_lineage != lineage:
        raise ValueError("candidateとFP32 referenceのtraining lineageが不一致です")
    evaluation_data = _predict_dataset_split(
        candidate,
        reference,
        dataset_paths=dataset_paths,
        split_manifest=split_manifest,
        split_name="validation",
        lineage=lineage,
        require_training_lineage_match=True,
    )
    return evaluate_validation_candidate(
        candidate,
        model_format=model_format,
        predictions=evaluation_data.predictions,
        lineage=lineage,
        fp32_reference_artifact_sha256=_sha256_file(reference),
        evaluation_dataset_fingerprint=evaluation_data.dataset_fingerprint,
        evaluation_split_fingerprint=evaluation_data.split_fingerprint,
        training_sample_ids=evaluation_data.training_sample_ids,
    )


def evaluate_frozen_test_candidate_from_dataset(
    selection: SelectedModelCandidate,
    fp32_reference_model: Path,
    *,
    dataset_paths: Sequence[Path],
    split_manifest: Path,
    allow_external_split: bool = False,
) -> CandidateEvaluation:
    """候補固定後だけpersisted test assignmentを実ORTで評価する.

    fine-tuneなどでtraining splitにtestがない場合は、明示的に
    ``allow_external_split=True`` として独立holdoutを指定する。training lineageは
    model artifactから変えず、release評価側のdataset/splitをreportへ別記録する。
    """

    candidate = selection.candidate.model_path.resolve(strict=True)
    reference = Path(fp32_reference_model).expanduser().resolve(strict=True)
    validation = selection.candidate.evaluation
    _validate_candidate_reference_artifacts(
        candidate, reference, validation.model_format
    )
    lineage = _evaluation_lineage(validation)
    if read_onnx_artifact_lineage(candidate) != lineage:
        raise ValueError("selected candidateのembedded lineageが不一致です")
    if read_onnx_artifact_lineage(reference) != lineage:
        raise ValueError("FP32 referenceのembedded lineageが不一致です")
    if _sha256_file(reference) != validation.fp32_reference_artifact_sha256:
        raise ValueError("validation時のFP32 reference artifactと不一致です")
    evaluation_data = _predict_dataset_split(
        candidate,
        reference,
        dataset_paths=dataset_paths,
        split_manifest=split_manifest,
        split_name="test",
        lineage=lineage,
        require_training_lineage_match=not allow_external_split,
    )
    return evaluate_frozen_test_candidate(
        selection,
        predictions=evaluation_data.predictions,
        evaluation_dataset_fingerprint=evaluation_data.dataset_fingerprint,
        evaluation_split_fingerprint=evaluation_data.split_fingerprint,
    )


def benchmark_model_candidate_from_dataset(
    model_path: Path,
    *,
    model_format: ModelFormat,
    dataset_paths: Sequence[Path],
    split_manifest: Path,
    power_condition: str,
    cooling_condition: str,
    warmup_iterations: int = BENCHMARK_WARMUP_ITERATIONS,
    measured_iterations: int = BENCHMARK_MEASURED_ITERATIONS,
) -> ModelBenchmarkResult:
    """Persisted validationから小/中/大/縦長/横長の代表sampleを選びPi計測する."""

    lineage = read_onnx_artifact_lineage(model_path)
    composite, samples, split = _resolve_evaluation_dataset(
        dataset_paths,
        split_manifest,
        lineage=lineage,
        require_training_lineage_match=True,
    )
    by_id = {sample.sample_id: sample for sample in samples}
    validation_samples = [by_id[sample_id] for sample_id in split.validation_sample_ids]
    if not validation_samples:
        raise ValueError("validation splitにbenchmark sampleがありません")
    representatives = _representative_samples(validation_samples)
    from torchvision.io import ImageReadMode, decode_image

    benchmark_samples: list[BenchmarkSample] = []
    for category, sample in representatives:
        pre = (
            decode_image(str(sample.pre_path), mode=ImageReadMode.RGB)
            .movedim(0, -1)
            .contiguous()
            .numpy()
        )
        post = (
            decode_image(str(sample.post_path), mode=ImageReadMode.RGB)
            .movedim(0, -1)
            .contiguous()
            .numpy()
        )
        benchmark_samples.append(
            BenchmarkSample(
                sample_id=sample.sample_id,
                category=category,
                pre_rgb=pre,
                post_rgb=post,
                pixel_per_mm=sample.pixel_per_mm,
            )
        )
    return benchmark_model_candidate(
        model_path,
        model_format=model_format,
        preprocess_schema=read_onnx_preprocess_schema(model_path),
        lineage=lineage,
        evaluation_dataset_fingerprint=composite.composite_fingerprint,
        evaluation_split_fingerprint=split.split_fingerprint,
        samples=benchmark_samples,
        power_condition=power_condition,
        cooling_condition=cooling_condition,
        warmup_iterations=warmup_iterations,
        measured_iterations=measured_iterations,
    )


def _predict_dataset_split(
    candidate_model: Path,
    reference_model: Path,
    *,
    dataset_paths: Sequence[Path],
    split_manifest: Path,
    split_name: Literal["validation", "test"],
    lineage: ArtifactLineage,
    require_training_lineage_match: bool,
) -> _DatasetSplitPredictions:
    import onnxruntime as ort
    import torch
    from torch.nn import functional as torch_functional

    from .data import load_preprocessed_sample

    _candidate_nodes = _check_onnx_model(candidate_model)
    reference_nodes = _check_onnx_model(reference_model)
    if "QuantizeLinear" in reference_nodes or "DequantizeLinear" in reference_nodes:
        raise ValueError("FP32 referenceにquantized operatorがあります")
    candidate_schema = read_onnx_preprocess_schema(candidate_model)
    reference_schema = read_onnx_preprocess_schema(reference_model)
    if _canonical_json(candidate_schema) != _canonical_json(reference_schema):
        raise ValueError("candidateとFP32 referenceのpreprocess schemaが不一致です")
    constraints = _image_constraints_from_schema(candidate_schema)
    composite, samples, split = _resolve_evaluation_dataset(
        dataset_paths,
        split_manifest,
        lineage=lineage,
        require_training_lineage_match=require_training_lineage_match,
    )
    by_id = {sample.sample_id: sample for sample in samples}
    ids = (
        split.validation_sample_ids
        if split_name == "validation"
        else split.test_sample_ids
    )
    if not ids:
        raise ValueError(f"{split_name} splitにsampleがありません")
    candidate_session = ort.InferenceSession(
        str(candidate_model), providers=["CPUExecutionProvider"]
    )
    reference_session = ort.InferenceSession(
        str(reference_model), providers=["CPUExecutionProvider"]
    )
    predictions: list[CandidatePrediction] = []
    for sample_id in ids:
        sample = by_id[sample_id]
        processed = load_preprocessed_sample(
            sample, constraints=constraints, training=False
        )
        candidate_mean, candidate_log_variance = _run_processed_onnx(
            candidate_session,
            processed.image_6ch,
            processed.valid_pixel_mask,
            processed.pixel_per_mm,
        )
        reference_mean, reference_log_variance = _run_processed_onnx(
            reference_session,
            processed.image_6ch,
            processed.valid_pixel_mask,
            processed.pixel_per_mm,
        )
        padded: dict[str, float] = {}
        height = int(processed.image_6ch.shape[1])
        width = int(processed.image_6ch.shape[2])
        for label, fraction, side in _PADDING_CONDITIONS:
            if side in {"top", "bottom"}:
                amount = max(1, round(height * fraction))
                padding = (0, 0, amount, 0) if side == "top" else (0, 0, 0, amount)
            else:
                amount = max(1, round(width * fraction))
                padding = (0, amount, 0, 0)
            padded_image = torch_functional.pad(processed.image_6ch, padding, value=0.0)
            padded_mask = torch_functional.pad(
                processed.valid_pixel_mask, padding, value=False
            )
            padded_mean, _ = _run_processed_onnx(
                candidate_session,
                padded_image,
                padded_mask,
                processed.pixel_per_mm,
            )
            padded[label] = padded_mean
        predictions.append(
            CandidatePrediction(
                sample_id=sample_id,
                target_volume_ul=sample.measured_volume_ul,
                mean_volume_ul=candidate_mean,
                log_variance_volume_ul2=candidate_log_variance,
                reference_mean_volume_ul=reference_mean,
                reference_log_variance_volume_ul2=reference_log_variance,
                sequence_group=sample.session_id,
                sequence_index=sample.pad_index * 1000 + sample.view_number,
                padded_mean_volume_ul=padded,
            )
        )
    return _DatasetSplitPredictions(
        predictions=tuple(predictions),
        dataset_fingerprint=composite.composite_fingerprint,
        split_fingerprint=split.split_fingerprint,
        training_sample_ids=tuple(split.train_sample_ids),
    )


def _resolve_evaluation_dataset(
    dataset_paths: Sequence[Path],
    split_manifest: Path,
    *,
    lineage: ArtifactLineage,
    require_training_lineage_match: bool,
) -> tuple[Any, tuple[Any, ...], Any]:
    from .data import (
        build_sample_index,
        load_split_manifest,
        resolve_dataset_inputs,
        validate_split_manifest,
    )

    paths = tuple(Path(path).expanduser().resolve() for path in dataset_paths)
    if not paths:
        raise ValueError("dataset_pathsを1件以上指定してください")
    if len(paths) == 1 and paths[0].is_file():
        composite = resolve_dataset_inputs(manifest=paths[0])
    else:
        composite = resolve_dataset_inputs(roots=paths)
    samples = build_sample_index(composite)
    split = load_split_manifest(
        Path(split_manifest).expanduser().resolve(strict=True),
        expected_composite_fingerprint=composite.composite_fingerprint,
    )
    validate_split_manifest(
        split,
        samples,
        expected_composite_fingerprint=composite.composite_fingerprint,
    )
    if (
        require_training_lineage_match
        and composite.composite_fingerprint != lineage.dataset_fingerprint
    ):
        raise ValueError("evaluation dataset fingerprintがtraining lineageと不一致です")
    if (
        require_training_lineage_match
        and split.split_fingerprint != lineage.split_fingerprint
    ):
        raise ValueError("evaluation split fingerprintがtraining lineageと不一致です")
    return composite, samples, split


def run_export_parity_from_dataset(
    weights_path: Path,
    fp32_model_path: Path,
    dataset_paths: Sequence[Path],
    split_manifest: Path,
    *,
    split_name: Literal["validation"] = "validation",
) -> ExportParityReport:
    """Evaluate eager-vs-exported FP32 ONNX on every persisted validation
    sample."""

    if split_name != "validation":
        raise ValueError("export parityはvalidation assignmentだけを許可します")
    import onnxruntime as ort
    import torch
    from torch.nn import functional as torch_functional

    from .data import load_preprocessed_sample

    weights_file = Path(weights_path).expanduser().resolve(strict=True)
    model_path = Path(fp32_model_path).expanduser().resolve(strict=True)
    weights = _load_weights(weights_file)
    nodes = _check_onnx_model(model_path)
    if "QuantizeLinear" in nodes or "DequantizeLinear" in nodes:
        raise ValueError("export parity referenceはFP32 ONNXである必要があります")
    metadata = _read_onnx_export_metadata(model_path)
    if metadata.get("artifact_role") != "export-fp32":
        raise ValueError("export parityには元のFP32 export artifactが必要です")
    expected_weights_sha256 = "sha256:" + _sha256_file(weights_file)
    if metadata.get("source_weights_sha256") != expected_weights_sha256:
        raise ValueError("FP32 exportとstrict weights artifactが一致しません")
    if _lineage_from_mapping(_required_mapping(metadata, "lineage")) != weights.lineage:
        raise ValueError("FP32 exportとstrict weights training lineageが一致しません")
    if _canonical_json(_required_mapping(metadata, "preprocess_schema")) != (
        _canonical_json(weights.preprocess_schema)
    ):
        raise ValueError("FP32 exportとstrict weights preprocess schemaが一致しません")

    composite, samples, split = _resolve_evaluation_dataset(
        dataset_paths,
        split_manifest,
        lineage=weights.lineage,
        require_training_lineage_match=True,
    )
    sample_ids = tuple(split.validation_sample_ids)
    training_sample_ids = tuple(split.train_sample_ids)
    if not sample_ids:
        raise ValueError("export parity validation splitにsampleがありません")
    by_id = {sample.sample_id: sample for sample in samples}
    constraints = _image_constraints_from_schema(weights.preprocess_schema)
    session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    sample_results: list[ExportParitySampleResult] = []
    first_processed: Any | None = None

    def compare(
        image_6ch: Any,
        valid_pixel_mask: Any,
        pixel_per_mm: float,
        *,
        context: str,
    ) -> tuple[float, float, float, float, bool]:
        eager_mean, eager_log_variance = weights.model(
            image_6ch[None, ...],
            valid_pixel_mask[None, ...],
            torch.tensor([[pixel_per_mm]], dtype=torch.float32),
        )
        eager_mean_value = float(eager_mean.item())
        eager_log_variance_value = float(
            eager_log_variance.item() + weights.uncertainty_log_variance_offset
        )
        actual_mean, actual_log_variance = _run_processed_onnx(
            session,
            image_6ch,
            valid_pixel_mask,
            pixel_per_mm,
        )
        values = (
            eager_mean_value,
            eager_log_variance_value,
            actual_mean,
            actual_log_variance,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError(f"export parity outputが非有限です: {context}")
        mean_error = abs(actual_mean - eager_mean_value)
        log_variance_error = abs(actual_log_variance - eager_log_variance_value)
        mean_tolerance = max(
            FP32_ABSOLUTE_TOLERANCE_UL,
            abs(eager_mean_value) * FP32_RELATIVE_TOLERANCE,
        )
        log_variance_tolerance = max(
            FP32_ABSOLUTE_TOLERANCE_UL,
            abs(eager_log_variance_value) * FP32_RELATIVE_TOLERANCE,
        )
        return (
            mean_error,
            mean_tolerance,
            log_variance_error,
            log_variance_tolerance,
            mean_error <= mean_tolerance
            and log_variance_error <= log_variance_tolerance,
        )

    weights.model.eval()
    with torch.no_grad():
        for sample_id in sample_ids:
            processed = load_preprocessed_sample(
                by_id[sample_id], constraints=constraints, training=False
            )
            if first_processed is None:
                first_processed = processed
            (
                mean_error,
                mean_tolerance,
                log_variance_error,
                log_variance_tolerance,
                passed,
            ) = compare(
                processed.image_6ch,
                processed.valid_pixel_mask,
                processed.pixel_per_mm,
                context=f"sample_id={sample_id}",
            )
            sample_results.append(
                ExportParitySampleResult(
                    sample_id=sample_id,
                    mean_absolute_error_ul=mean_error,
                    mean_tolerance_ul=mean_tolerance,
                    log_variance_absolute_error=log_variance_error,
                    log_variance_tolerance=log_variance_tolerance,
                    passed=passed,
                )
            )
        if first_processed is None:  # pragma: no cover - guarded by sample_ids
            raise AssertionError(
                "export parity validation sampleがmaterializeされませんでした"
            )
        base_image = first_processed.image_6ch[None, ...]
        base_mask = first_processed.valid_pixel_mask[None, ...].to(dtype=torch.float32)
        shape_results: list[ExportParityShapeResult] = []
        for case_name, (height, width) in _EXPORT_PARITY_SHAPES:
            image = torch_functional.interpolate(
                base_image,
                size=(height, width),
                mode="bilinear",
                align_corners=False,
            )[0]
            mask = torch_functional.interpolate(
                base_mask,
                size=(height, width),
                mode="nearest",
            )[0].to(dtype=torch.bool)
            (
                mean_error,
                mean_tolerance,
                log_variance_error,
                log_variance_tolerance,
                passed,
            ) = compare(
                image,
                mask,
                first_processed.pixel_per_mm,
                context=f"shape_case={case_name}",
            )
            shape_results.append(
                ExportParityShapeResult(
                    case_name=case_name,
                    image_height=height,
                    image_width=width,
                    mean_absolute_error_ul=mean_error,
                    mean_tolerance_ul=mean_tolerance,
                    log_variance_absolute_error=log_variance_error,
                    log_variance_tolerance=log_variance_tolerance,
                    passed=passed,
                )
            )
    all_results = (*sample_results, *shape_results)
    report = ExportParityReport(
        kind=EXPORT_PARITY_REPORT_KIND,
        schema_version=EXPORT_PARITY_REPORT_SCHEMA_VERSION,
        weights_sha256=expected_weights_sha256,
        fp32_model_artifact_sha256=_sha256_file(model_path),
        training_protocol_fingerprint=(weights.lineage.training_protocol_fingerprint),
        dataset_fingerprint=composite.composite_fingerprint,
        split_fingerprint=split.split_fingerprint,
        split_name="validation",
        sample_ids=sample_ids,
        training_sample_ids=training_sample_ids,
        sample_results=tuple(sample_results),
        shape_results=tuple(shape_results),
        maximum_mean_absolute_error_ul=max(
            item.mean_absolute_error_ul for item in all_results
        ),
        maximum_log_variance_absolute_error=max(
            item.log_variance_absolute_error for item in all_results
        ),
        success=all(item.passed for item in all_results),
        content_sha256="",
    )
    content = report.to_dict()
    del content["content_sha256"]
    finalized = attrs.evolve(
        report,
        content_sha256="sha256:" + _canonical_json_sha256(content),
    )
    validate_export_parity_report(finalized)
    return finalized


def run_export_parity_from_formal_weights(
    formal_weights: FormalTrainingWeights,
    fp32_model_path: Path,
    dataset_paths: Sequence[Path],
    split_manifest: Path,
    *,
    split_name: Literal["validation"] = "validation",
) -> ExportParityReport:
    """Generate release parity only from a live-verified training artifact."""

    _validate_formal_training_weights(formal_weights, formal_weights.weights_path)
    metadata = _read_onnx_export_metadata(
        Path(fp32_model_path).expanduser().resolve(strict=True)
    )
    if metadata.get("training_weights_attestation") != (
        formal_weights.attestation.to_dict()
    ):
        raise ValueError("FP32 exportのformal training attestationが入力と不一致です")
    return run_export_parity_from_dataset(
        formal_weights.weights_path,
        fp32_model_path,
        dataset_paths,
        split_manifest,
        split_name=split_name,
    )


def run_compile_parity_from_dataset(
    weights_path: Path,
    dataset_paths: Sequence[Path],
    split_manifest: Path,
    *,
    config: CompileParityConfig | None = None,
) -> CompileParityReport:
    """Strict weightsとpersisted train assignmentからcompile証跡を生成する."""

    import torch
    from torch.nn import functional as torch_functional

    from .data import PasteVolumeDataset, collate_paste_volume

    weights = _load_weights(weights_path)
    _, samples, split = _resolve_evaluation_dataset(
        dataset_paths,
        split_manifest,
        lineage=weights.lineage,
        require_training_lineage_match=True,
    )
    by_id = {sample.sample_id: sample for sample in samples}
    train_samples = tuple(by_id[sample_id] for sample_id in split.train_sample_ids)
    if not train_samples:
        raise ValueError("compile parityにはpersisted train sampleが必要です")
    constraints = _image_constraints_from_schema(weights.preprocess_schema)
    materialized = PasteVolumeDataset(
        train_samples[: min(2, len(train_samples))],
        constraints=constraints,
        training=True,
        global_seed=0,
    )
    items = [materialized[index] for index in range(len(materialized))]
    actual = collate_paste_volume(
        items,
        training=True,
        stride=constraints.stride,
    )
    training_batch = CompileParityBatch(
        case_id="training-batch-persisted-epoch-0",
        shape_case="training-batch",
        image_6ch=actual.image_6ch,
        valid_pixel_mask=actual.valid_pixel_mask,
        pixel_per_mm=actual.pixel_per_mm,
        target_volume_ul=actual.target_volume_ul.reshape(-1, 1),
        sample_weight=actual.loss_weight.reshape(-1, 1),
        sample_ids=actual.sample_ids,
    )
    base_image = actual.image_6ch[:1]
    base_mask = actual.valid_pixel_mask[:1].to(dtype=torch.float32)
    fixed_batches: list[CompileParityBatch] = []
    for shape_case, (height, width) in (
        ("minimum", (32, 32)),
        ("maximum-area", (512, 512)),
        ("portrait", (1024, 256)),
        ("landscape", (256, 1024)),
    ):
        image = torch_functional.interpolate(
            base_image,
            size=(height, width),
            mode="bilinear",
            align_corners=False,
        )
        mask = torch_functional.interpolate(
            base_mask,
            size=(height, width),
            mode="nearest",
        ).to(dtype=torch.bool)
        fixed_batches.append(
            CompileParityBatch(
                case_id=f"{shape_case}-{height}x{width}",
                shape_case=cast(Any, shape_case),
                image_6ch=image,
                valid_pixel_mask=mask,
                pixel_per_mm=actual.pixel_per_mm[:1],
                target_volume_ul=actual.target_volume_ul[:1].reshape(1, 1),
                sample_weight=actual.loss_weight[:1].reshape(1, 1),
                sample_ids=(f"{actual.sample_ids[0]}:{shape_case}",),
            )
        )
    return run_compile_parity(
        weights.model,
        (*fixed_batches, training_batch),
        weights_path=Path(weights_path).expanduser().resolve(strict=True),
        training_protocol_fingerprint=(weights.lineage.training_protocol_fingerprint),
        dataset_fingerprint=weights.lineage.dataset_fingerprint,
        split_fingerprint=weights.lineage.split_fingerprint,
        config=config,
    )


def run_compile_parity_from_formal_weights(
    formal_weights: FormalTrainingWeights,
    dataset_paths: Sequence[Path],
    split_manifest: Path,
    *,
    config: CompileParityConfig | None = None,
) -> CompileParityReport:
    """Generate compile evidence only from live-verified training weights."""

    _validate_formal_training_weights(formal_weights, formal_weights.weights_path)
    return run_compile_parity_from_dataset(
        formal_weights.weights_path,
        dataset_paths,
        split_manifest,
        config=config,
    )


def _run_processed_onnx(
    session: Any,
    image: Any,
    mask: Any,
    pixel_per_mm: float,
) -> tuple[float, float]:
    result = session.run(
        ["mean_volume_ul", "log_variance_volume_ul2"],
        {
            "image_6ch": image.detach().cpu().float().numpy()[None, ...],
            "valid_pixel_mask": mask.detach().cpu().numpy()[None, ...].astype(np.bool_),
            "pixel_per_mm": np.asarray([[pixel_per_mm]], dtype=np.float32),
        },
    )
    if len(result) != 2:
        raise RuntimeError("ONNX model output数が不正です")
    mean = float(np.asarray(result[0]).item())
    log_variance = float(np.asarray(result[1]).item())
    if not math.isfinite(mean) or mean <= 0 or not math.isfinite(log_variance):
        raise RuntimeError(
            "ONNX model outputが正の有限mean/有限log-varianceではありません"
        )
    return mean, log_variance


def _representative_samples(
    samples: Sequence[Any],
) -> tuple[tuple[BenchmarkCategory, Any], ...]:
    if len({sample.sample_id for sample in samples}) < len(_BENCHMARK_CATEGORIES):
        raise ValueError("benchmarkの5 categoryには5件以上の異なるsampleが必要です")
    portrait_candidates = [sample for sample in samples if sample.aspect_ratio < 1.0]
    landscape_candidates = [sample for sample in samples if sample.aspect_ratio > 1.0]
    if not portrait_candidates or not landscape_candidates:
        raise ValueError("benchmarkにはportraitとlandscape sampleが必要です")
    portrait = min(
        portrait_candidates,
        key=lambda sample: (sample.aspect_ratio, sample.sample_id),
    )
    landscape = max(
        landscape_candidates,
        key=lambda sample: (sample.aspect_ratio, sample.sample_id),
    )
    reserved = {portrait.sample_id, landscape.sample_id}
    area_candidates = sorted(
        (sample for sample in samples if sample.sample_id not in reserved),
        key=lambda sample: (sample.image_area_pixels, sample.sample_id),
    )
    distinct_areas = sorted({sample.image_area_pixels for sample in area_candidates})
    if len(distinct_areas) < 3:
        raise ValueError(
            "benchmarkのsmall/medium/largeには3段階の異なる画像面積が必要です"
        )
    small_area = distinct_areas[0]
    medium_area = distinct_areas[len(distinct_areas) // 2]
    large_area = distinct_areas[-1]

    def first_at_area(area: int) -> Any:
        return next(
            sample for sample in area_candidates if sample.image_area_pixels == area
        )

    return (
        ("small", first_at_area(small_area)),
        ("medium", first_at_area(medium_area)),
        ("large", first_at_area(large_area)),
        ("portrait", portrait),
        ("landscape", landscape),
    )


def _run_benchmark_sample(
    session: Any,
    sample: BenchmarkSample,
    preprocess_schema: Mapping[str, Any],
) -> None:
    from .data import ImageConstraints, preprocess_rgb_pair

    _validate_rgb_array(sample.pre_rgb, "pre_rgb")
    _validate_rgb_array(sample.post_rgb, "post_rgb")
    if sample.pre_rgb.shape != sample.post_rgb.shape:
        raise ValueError("benchmark pre/post shapeが一致しません")
    if not _is_positive_finite(sample.pixel_per_mm):
        raise ValueError("benchmark pixel_per_mmは正の有限値が必要です")
    raw = _required_mapping(preprocess_schema, "image_constraints")
    processed = preprocess_rgb_pair(
        sample.pre_rgb,
        sample.post_rgb,
        sample.pixel_per_mm,
        constraints=ImageConstraints(
            min_size=int(raw["min_size"]),
            max_size=int(raw["max_size"]),
            max_pixels=int(raw["max_pixels"]),
            stride=int(raw["stride"]),
            normalization_epsilon=float(raw["normalization_epsilon"]),
        ),
    )
    result = session.run(
        ["mean_volume_ul", "log_variance_volume_ul2"],
        {
            "image_6ch": processed.image_6ch.detach().cpu().float().numpy()[None, ...],
            "valid_pixel_mask": processed.valid_pixel_mask.detach()
            .cpu()
            .numpy()[None, ...]
            .astype(np.bool_),
            "pixel_per_mm": np.asarray([[processed.pixel_per_mm]], dtype=np.float32),
        },
    )
    if len(result) != 2:
        raise RuntimeError("benchmark model output数が不正です")
    mean = float(np.asarray(result[0]).item())
    log_variance = float(np.asarray(result[1]).item())
    if not math.isfinite(mean) or mean <= 0 or not math.isfinite(log_variance):
        raise RuntimeError("benchmark modelが不正な出力を返しました")


def _platform_model() -> str:
    path = Path("/proc/device-tree/model")
    if path.is_file():
        try:
            return path.read_bytes().rstrip(b"\x00").decode("utf-8")
        except (OSError, UnicodeDecodeError):
            pass
    return f"{platform.system()} {platform.machine()}"


def _process_start_monotonic_ns() -> tuple[int, str]:
    stat_path = Path("/proc/self/stat")
    try:
        raw = stat_path.read_text(encoding="utf-8")
        fields_after_command = raw[raw.rindex(")") + 2 :].split()
        start_ticks = int(fields_after_command[19])
        clock_ticks = int(os.sysconf("SC_CLK_TCK"))
        if start_ticks >= 0 and clock_ticks > 0:
            return (
                start_ticks * 1_000_000_000 // clock_ticks,
                "process-start:/proc/self/stat",
            )
    except (OSError, ValueError, IndexError):
        pass
    return _MODULE_MONOTONIC_ORIGIN_NS, "module-import:fallback"


def _os_id() -> str:
    path = Path("/etc/os-release")
    try:
        values = {
            key: value.strip().strip('"')
            for line in path.read_text(encoding="utf-8").splitlines()
            if "=" in line
            for key, value in (line.split("=", 1),)
        }
    except OSError:
        return platform.system().lower() or "unknown"
    return values.get("ID", platform.system().lower() or "unknown")


def _cpu_governor() -> str:
    path = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return "unavailable"
    return value or "unavailable"


def evaluate_validation_candidate(
    model_path: Path,
    *,
    model_format: ModelFormat,
    predictions: Sequence[CandidatePrediction],
    lineage: ArtifactLineage,
    fp32_reference_artifact_sha256: str,
    evaluation_dataset_fingerprint: str,
    evaluation_split_fingerprint: str,
    training_sample_ids: Sequence[str],
) -> CandidateEvaluation:
    """validationだけで候補gateと不確かさthresholdを決める."""

    path = Path(model_path).expanduser().resolve(strict=True)
    artifact_hash = _sha256_file(path)
    embedded = _validate_candidate_artifact(path, model_format)
    if _lineage_from_mapping(_required_mapping(embedded, "lineage")) != lineage:
        raise ValueError("validation modelのembedded lineageが不一致です")
    _validate_source_export_hash(
        embedded,
        candidate_artifact_sha256=artifact_hash,
        fp32_reference_artifact_sha256=fp32_reference_artifact_sha256,
    )
    candidate_id = _candidate_id(model_format, artifact_hash)
    return _evaluate_predictions(
        phase="validation",
        candidate_id=candidate_id,
        model_format=model_format,
        model_artifact_sha256=artifact_hash,
        predictions=predictions,
        lineage=lineage,
        evaluation_dataset_fingerprint=evaluation_dataset_fingerprint,
        evaluation_split_fingerprint=evaluation_split_fingerprint,
        training_sample_ids=training_sample_ids,
        fp32_reference_artifact_sha256=fp32_reference_artifact_sha256,
        fixed_threshold=None,
    )


def read_onnx_artifact_lineage(model_path: Path) -> ArtifactLineage:
    """ONNX artifactへbind済みのtraining lineageを読む."""

    path = Path(model_path).expanduser().resolve(strict=True)
    return _lineage_from_mapping(
        _required_mapping(_read_onnx_export_metadata(path), "lineage")
    )


def read_onnx_preprocess_schema(model_path: Path) -> Mapping[str, Any]:
    """ONNX artifactへbind済みのpreprocess schemaを読む."""

    path = Path(model_path).expanduser().resolve(strict=True)
    schema = _required_mapping(_read_onnx_export_metadata(path), "preprocess_schema")
    _validate_canonical_preprocess_schema(schema)
    return schema


def save_candidate_predictions(
    path: Path, predictions: Sequence[CandidatePrediction]
) -> Path:
    """候補評価inputをversioned JSONへ保存する."""

    output = Path(path).expanduser().resolve()
    _atomic_json_new(
        output,
        {
            "schema_version": EVALUATION_SCHEMA_VERSION,
            "predictions": [attrs.asdict(item) for item in predictions],
        },
    )
    return output


def load_candidate_predictions(path: Path) -> tuple[CandidatePrediction, ...]:
    raw = _read_json(Path(path).expanduser().resolve(strict=True))
    if raw.get("schema_version") != EVALUATION_SCHEMA_VERSION:
        raise ValueError("未知のcandidate prediction schemaです")
    values = raw.get("predictions")
    if not isinstance(values, list):
        raise ValueError("predictionsはarrayが必要です")
    result: list[CandidatePrediction] = []
    expected = {field.name for field in attrs.fields(CandidatePrediction)}
    for index, value in enumerate(values):
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError(f"prediction[{index}]のkey集合が不正です")
        padding = value.get("padded_mean_volume_ul")
        if not isinstance(padding, dict) or not all(
            isinstance(key, str) and _is_finite_number(item)
            for key, item in padding.items()
        ):
            raise ValueError(f"prediction[{index}]のpadding値が不正です")
        result.append(CandidatePrediction(**value))
    if not result:
        raise ValueError("predictionがありません")
    return tuple(result)


def save_candidate_evaluation(path: Path, evaluation: CandidateEvaluation) -> Path:
    output = Path(path).expanduser().resolve()
    _atomic_json_new(output, evaluation.to_dict())
    return output


def load_candidate_evaluation(path: Path) -> CandidateEvaluation:
    raw = dict(_read_json(Path(path).expanduser().resolve(strict=True)))
    expected = {field.name for field in attrs.fields(CandidateEvaluation)}
    if set(raw) != expected:
        raise ValueError("candidate evaluationのkey集合が不正です")
    failures = raw.get("gate_failures")
    if not isinstance(failures, list) or not all(
        isinstance(item, str) for item in failures
    ):
        raise ValueError("gate_failuresは文字列arrayが必要です")
    raw["gate_failures"] = tuple(failures)
    for key in ("evaluated_sample_ids", "training_sample_ids"):
        sample_ids = raw.get(key)
        if not isinstance(sample_ids, list) or not all(
            isinstance(item, str) and item for item in sample_ids
        ):
            raise ValueError(f"{key}は空でない文字列arrayが必要です")
        raw[key] = tuple(sample_ids)
    try:
        result = CandidateEvaluation(**raw)
    except TypeError as exc:
        raise ValueError(f"candidate evaluation schemaが不正です: {exc}") from exc
    if result.phase not in ("validation", "frozen_test"):
        raise ValueError("candidate evaluation phaseが不正です")
    if result.model_format not in ("onnx-fp32", "onnx-int8-qdq"):
        raise ValueError("candidate evaluation model formatが不正です")
    return result


def save_model_benchmark_result(path: Path, benchmark: ModelBenchmarkResult) -> Path:
    output = Path(path).expanduser().resolve()
    _atomic_json_new(output, benchmark.to_dict())
    return output


def load_model_benchmark_result(path: Path) -> ModelBenchmarkResult:
    raw = dict(_read_json(Path(path).expanduser().resolve(strict=True)))
    expected = {field.name for field in attrs.fields(ModelBenchmarkResult)}
    if set(raw) != expected:
        raise ValueError("benchmark reportのkey集合が不正です")
    sample_ids = raw.get("benchmark_sample_ids")
    if not isinstance(sample_ids, list) or not all(
        isinstance(item, str) and item for item in sample_ids
    ):
        raise ValueError("benchmark_sample_idsは空でない文字列arrayが必要です")
    raw["benchmark_sample_ids"] = tuple(sample_ids)
    category_values = raw.get("category_results")
    if not isinstance(category_values, list):
        raise ValueError("benchmark category_resultsはarrayが必要です")
    category_expected = {field.name for field in attrs.fields(BenchmarkCategoryResult)}
    category_results: list[BenchmarkCategoryResult] = []
    for index, value in enumerate(category_values):
        if not isinstance(value, dict) or set(value) != category_expected:
            raise ValueError(f"benchmark category_results[{index}]のkey集合が不正です")
        try:
            category_results.append(BenchmarkCategoryResult(**value))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"benchmark category_results[{index}]が不正です: {exc}"
            ) from exc
    raw["category_results"] = tuple(category_results)
    try:
        result = ModelBenchmarkResult(**raw)
    except TypeError as exc:
        raise ValueError(f"benchmark report schemaが不正です: {exc}") from exc
    if result.schema_version != BENCHMARK_SCHEMA_VERSION:
        raise ValueError("未知のbenchmark report schemaです")
    _validate_benchmark_iterations(result)
    return result


def validate_export_parity_report(report: ExportParityReport) -> None:
    """Validate a complete export parity report and its canonical content
    hash."""

    if not _is_prefixed_sha256(report.content_sha256):
        raise ValueError("export parity content_sha256が不正です")
    payload = report.to_dict()
    del payload["content_sha256"]
    expected = "sha256:" + _canonical_json_sha256(payload)
    if report.content_sha256 != expected:
        raise ValueError("export parity content_sha256が内容と一致しません")


def export_parity_report_from_dict(raw: Mapping[str, Any]) -> ExportParityReport:
    """Parse the strict v1 export parity schema."""

    expected = {
        "kind",
        "schema_version",
        "weights_sha256",
        "fp32_model_artifact_sha256",
        "training_protocol_fingerprint",
        "dataset_fingerprint",
        "split_fingerprint",
        "split_name",
        "sample_ids",
        "training_sample_ids",
        "sample_results",
        "shape_results",
        "maximum_mean_absolute_error_ul",
        "maximum_log_variance_absolute_error",
        "success",
        "content_sha256",
    }
    if set(raw) != expected:
        raise ValueError("export parity reportのkey集合が不正です")
    sample_ids = raw.get("sample_ids")
    training_sample_ids = raw.get("training_sample_ids")
    samples_raw = raw.get("sample_results")
    shapes_raw = raw.get("shape_results")
    if (
        not isinstance(sample_ids, list)
        or not all(isinstance(item, str) for item in sample_ids)
        or not isinstance(training_sample_ids, list)
        or not all(isinstance(item, str) for item in training_sample_ids)
        or not isinstance(samples_raw, list)
        or not isinstance(shapes_raw, list)
    ):
        raise ValueError("export parity sample assignmentが不正です")
    sample_expected = {
        "sample_id",
        "mean_absolute_error_ul",
        "mean_tolerance_ul",
        "log_variance_absolute_error",
        "log_variance_tolerance",
        "passed",
    }
    sample_results: list[ExportParitySampleResult] = []
    for index, value in enumerate(samples_raw):
        if not isinstance(value, dict) or set(value) != sample_expected:
            raise ValueError(f"export parity sample_results[{index}]が不正です")
        try:
            sample_results.append(ExportParitySampleResult(**value))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"export parity sample_results[{index}]が不正です: {exc}"
            ) from exc
    shape_expected = {
        "case_name",
        "image_height",
        "image_width",
        "mean_absolute_error_ul",
        "mean_tolerance_ul",
        "log_variance_absolute_error",
        "log_variance_tolerance",
        "passed",
    }
    shape_results: list[ExportParityShapeResult] = []
    for index, value in enumerate(shapes_raw):
        if not isinstance(value, dict) or set(value) != shape_expected:
            raise ValueError(f"export parity shape_results[{index}]が不正です")
        try:
            shape_results.append(ExportParityShapeResult(**value))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"export parity shape_results[{index}]が不正です: {exc}"
            ) from exc
    values = dict(raw)
    values["sample_ids"] = tuple(sample_ids)
    values["training_sample_ids"] = tuple(training_sample_ids)
    values["sample_results"] = tuple(sample_results)
    values["shape_results"] = tuple(shape_results)
    try:
        report = ExportParityReport(**values)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"export parity report schemaが不正です: {exc}") from exc
    validate_export_parity_report(report)
    return report


def save_export_parity_report(path: Path, report: ExportParityReport) -> Path:
    """Persist a verified report with create-only semantics."""

    validate_export_parity_report(report)
    output = Path(path).expanduser().resolve()
    _atomic_json_new(output, report.to_dict())
    return output


def load_export_parity_report(path: Path) -> ExportParityReport:
    """Load a persisted export parity report with strict schema/hash checks."""

    return export_parity_report_from_dict(
        _read_json(Path(path).expanduser().resolve(strict=True))
    )


def save_model_candidate_validation(
    path: Path, candidate: ModelCandidateValidation
) -> Path:
    output = Path(path).expanduser().resolve()
    _atomic_json_new(output, candidate.to_dict())
    return output


def load_model_candidate_validation(path: Path) -> ModelCandidateValidation:
    raw = _read_json(Path(path).expanduser().resolve(strict=True))
    if set(raw) != {
        "evaluation",
        "benchmark",
        "compile_parity",
        "export_parity",
        "model_path",
        "artifact_size_bytes",
        "dependency_complexity_rank",
        "gate_passed",
    }:
        raise ValueError("candidate validationのkey集合が不正です")
    evaluation_raw = raw.get("evaluation")
    benchmark_raw = raw.get("benchmark")
    compile_raw = raw.get("compile_parity")
    export_raw = raw.get("export_parity")
    if (
        not isinstance(evaluation_raw, dict)
        or not isinstance(benchmark_raw, dict)
        or not isinstance(compile_raw, dict)
        or not isinstance(export_raw, dict)
    ):
        raise ValueError("candidate validation reportが不正です")
    compile_parity = compile_parity_report_from_dict(compile_raw)
    export_parity = export_parity_report_from_dict(export_raw)
    with tempfile.TemporaryDirectory(prefix="paste-volume-report-load-") as temporary:
        evaluation_path = Path(temporary) / "evaluation.json"
        benchmark_path = Path(temporary) / "benchmark.json"
        _write_json(evaluation_path, evaluation_raw)
        _write_json(benchmark_path, benchmark_raw)
        evaluation = load_candidate_evaluation(evaluation_path)
        benchmark = load_model_benchmark_result(benchmark_path)
    model_path = raw.get("model_path")
    rank = raw.get("dependency_complexity_rank")
    if not isinstance(model_path, str) or type(rank) is not int:
        raise ValueError("candidate validation model path/rankが不正です")
    result = build_model_candidate_validation(
        evaluation,
        benchmark,
        compile_parity,
        export_parity,
        Path(model_path),
        dependency_complexity_rank=rank,
    )
    if raw.get("artifact_size_bytes") != result.artifact_size_bytes:
        raise ValueError("candidate artifact sizeが変更されています")
    if raw.get("gate_passed") is not result.gate_passed:
        raise ValueError("candidate gate statusが不一致です")
    return result


def save_selected_model_candidate(
    path: Path, selection: SelectedModelCandidate
) -> Path:
    output = Path(path).expanduser().resolve()
    _atomic_json_new(output, selection.to_dict())
    return output


def load_selected_model_candidate(path: Path) -> SelectedModelCandidate:
    raw = _read_json(Path(path).expanduser().resolve(strict=True))
    if set(raw) != {"candidate", "compared_candidate_ids"}:
        raise ValueError("selected candidateのkey集合が不正です")
    candidate_raw = raw.get("candidate")
    ids = raw.get("compared_candidate_ids")
    if (
        not isinstance(candidate_raw, dict)
        or not isinstance(ids, list)
        or not all(isinstance(item, str) for item in ids)
    ):
        raise ValueError("selected candidate schemaが不正です")
    with tempfile.TemporaryDirectory(
        prefix="paste-volume-selection-load-"
    ) as temporary:
        candidate_path = Path(temporary) / "candidate.json"
        _write_json(candidate_path, candidate_raw)
        candidate = load_model_candidate_validation(candidate_path)
    selection = SelectedModelCandidate(
        candidate=candidate, compared_candidate_ids=tuple(ids)
    )
    if candidate.candidate_id not in selection.compared_candidate_ids:
        raise ValueError("selected candidateが比較候補に含まれません")
    return selection


def save_finalized_model_candidate(
    path: Path, finalized: FinalizedModelCandidate
) -> Path:
    output = Path(path).expanduser().resolve()
    _atomic_json_new(output, finalized.to_dict())
    return output


def load_finalized_model_candidate(path: Path) -> FinalizedModelCandidate:
    raw = _read_json(Path(path).expanduser().resolve(strict=True))
    if set(raw) != {"selection", "frozen_test"}:
        raise ValueError("finalized candidateのkey集合が不正です")
    selection_raw = raw.get("selection")
    frozen_raw = raw.get("frozen_test")
    if not isinstance(selection_raw, dict) or not isinstance(frozen_raw, dict):
        raise ValueError("finalized candidate schemaが不正です")
    with tempfile.TemporaryDirectory(prefix="paste-volume-final-load-") as temporary:
        selection_path = Path(temporary) / "selection.json"
        frozen_path = Path(temporary) / "frozen.json"
        _write_json(selection_path, selection_raw)
        _write_json(frozen_path, frozen_raw)
        selection = load_selected_model_candidate(selection_path)
        frozen = load_candidate_evaluation(frozen_path)
    return finalize_selected_model_candidate(selection, frozen)


def build_model_candidate_validation(
    evaluation: CandidateEvaluation,
    benchmark: ModelBenchmarkResult,
    compile_parity: CompileParityReport,
    export_parity: ExportParityReport,
    model_path: Path,
    *,
    dependency_complexity_rank: int | None = None,
) -> ModelCandidateValidation:
    """validation、benchmark、実artifactをstrictにbindする."""

    if evaluation.phase != "validation":
        raise ValueError("候補比較にはvalidation evaluationだけを使用できます")
    path = Path(model_path).expanduser().resolve(strict=True)
    metadata = _validate_candidate_artifact(path, evaluation.model_format)
    _validate_release_compile_parity(compile_parity)
    validate_export_parity_report(export_parity)
    if not export_parity.success:
        raise ValueError("export parity gateを通過していません")
    actual_hash = _sha256_file(path)
    if actual_hash != evaluation.model_artifact_sha256:
        raise ValueError("validation evaluationとmodel artifact hashが不一致です")
    if benchmark.candidate_id != evaluation.candidate_id:
        raise ValueError("benchmarkとvalidationのcandidate_idが不一致です")
    if benchmark.model_artifact_sha256 != actual_hash:
        raise ValueError("benchmarkとmodel artifact hashが不一致です")
    _validate_benchmark_iterations(benchmark)
    _validate_source_export_hash(
        metadata,
        candidate_artifact_sha256=actual_hash,
        fp32_reference_artifact_sha256=(evaluation.fp32_reference_artifact_sha256),
    )
    lineage = _lineage_from_mapping(_required_mapping(metadata, "lineage"))
    _formal_training_attestation_from_metadata(metadata, lineage)
    source_weights_sha256 = metadata.get("source_weights_sha256")
    if compile_parity.provenance.weights_sha256 != source_weights_sha256:
        raise ValueError("compile parityのsource weightsが候補modelと不一致です")
    if export_parity.weights_sha256 != source_weights_sha256:
        raise ValueError("export parityのsource weightsが候補modelと不一致です")
    if (
        export_parity.fp32_model_artifact_sha256
        != evaluation.fp32_reference_artifact_sha256
    ):
        raise ValueError("export parityのFP32 sourceがvalidationと不一致です")
    export_checks = (
        (
            "training_protocol_fingerprint",
            export_parity.training_protocol_fingerprint,
            lineage.training_protocol_fingerprint,
        ),
        (
            "dataset_fingerprint",
            export_parity.dataset_fingerprint,
            evaluation.dataset_fingerprint,
        ),
        (
            "split_fingerprint",
            export_parity.split_fingerprint,
            evaluation.split_fingerprint,
        ),
        (
            "sample_ids",
            export_parity.sample_ids,
            evaluation.evaluated_sample_ids,
        ),
        (
            "training_sample_ids",
            export_parity.training_sample_ids,
            evaluation.training_sample_ids,
        ),
    )
    for name, actual, expected in export_checks:
        if actual != expected:
            raise ValueError(f"export parity validation assignmentが不一致です: {name}")
    compile_checks = (
        (
            "training_protocol_fingerprint",
            compile_parity.provenance.training_protocol_fingerprint,
            lineage.training_protocol_fingerprint,
        ),
        (
            "dataset_fingerprint",
            compile_parity.provenance.dataset_fingerprint,
            lineage.dataset_fingerprint,
        ),
        (
            "split_fingerprint",
            compile_parity.provenance.split_fingerprint,
            lineage.split_fingerprint,
        ),
    )
    for name, actual, expected in compile_checks:
        if actual != expected:
            raise ValueError(f"compile parity training lineageが不一致です: {name}")
    for name in (
        "source_run_id",
        "source_checkpoint_sha256",
        "source_checkpoint_role",
        "training_dataset_fingerprint",
        "training_split_fingerprint",
        "training_protocol_fingerprint",
        "dataset_fingerprint",
        "split_fingerprint",
        "parent_run_id",
        "parent_checkpoint_id",
    ):
        if getattr(benchmark, name) != getattr(evaluation, name):
            raise ValueError(f"benchmark lineageが不一致です: {name}")
    if not set(benchmark.benchmark_sample_ids) <= set(evaluation.evaluated_sample_ids):
        raise ValueError("benchmark sampleがvalidation assignment外です")
    rank = dependency_complexity_rank
    if rank is None:
        rank = 0 if evaluation.model_format == "onnx-fp32" else 1
    if type(rank) is not int or rank < 0:
        raise ValueError("dependency_complexity_rankは0以上の整数が必要です")
    return ModelCandidateValidation(
        evaluation=evaluation,
        benchmark=benchmark,
        compile_parity=compile_parity,
        export_parity=export_parity,
        model_path=path,
        artifact_size_bytes=path.stat().st_size,
        dependency_complexity_rank=rank,
    )


def _validate_release_compile_parity(report: CompileParityReport) -> None:
    validate_compile_parity_report(report)
    if not report.success:
        raise ValueError("compile parity gateを通過していません")
    if (
        report.runtime.backend != "inductor"
        or report.runtime.mode != "default"
        or report.runtime.graph_break_count != 0
    ):
        raise ValueError(
            "release compile parityにはinductor/default/graph_break_count=0が必要です"
        )


def _formal_training_attestation_from_metadata(
    metadata: Mapping[str, Any],
    lineage: ArtifactLineage,
) -> FormalArtifactAttestation:
    raw = metadata.get("training_weights_attestation")
    if not isinstance(raw, Mapping):
        raise ValueError(
            "release candidateにはformal training weights attestationが必要です"
        )
    try:
        attestation = FormalArtifactAttestation.from_dict(raw)
    except ValueError as exc:
        raise ValueError("formal training weights attestationが不正です") from exc
    if (
        attestation.status != "FINISHED"
        or attestation.output_kind != "file"
        or attestation.output_fingerprint != metadata.get("source_weights_sha256")
        or attestation.run_id != lineage.source_run_id
        or attestation.run_kind not in ("base-train", "finetune")
    ):
        raise ValueError("formal training weights attestationがlineageと不一致です")
    return attestation


def select_model_candidate(
    candidates: Sequence[ModelCandidateValidation],
) -> SelectedModelCandidate:
    """Validation gate通過候補からPi p95で固定し、5%未満なら単純さを選ぶ."""

    if not candidates:
        raise ValueError("候補がありません")
    ids = [candidate.candidate_id for candidate in candidates]
    if len(ids) != len(set(ids)):
        raise ValueError("candidate_idが重複しています")
    reference_evaluation = candidates[0].evaluation
    reference_compile_hash = candidates[0].compile_parity.content_sha256
    if any(
        candidate.compile_parity.content_sha256 != reference_compile_hash
        for candidate in candidates[1:]
    ):
        raise ValueError("候補間のcompile parity証跡が不一致です")
    reference_export_hash = candidates[0].export_parity.content_sha256
    if any(
        candidate.export_parity.content_sha256 != reference_export_hash
        for candidate in candidates[1:]
    ):
        raise ValueError("候補間のexport parity証跡が不一致です")
    shared_evaluation_fields = (
        "source_run_id",
        "source_checkpoint_sha256",
        "source_checkpoint_role",
        "training_dataset_fingerprint",
        "training_split_fingerprint",
        "training_protocol_fingerprint",
        "parent_run_id",
        "parent_checkpoint_id",
        "dataset_fingerprint",
        "split_fingerprint",
        "fp32_reference_artifact_sha256",
        "training_sample_ids",
        "evaluated_sample_ids",
    )
    for candidate in candidates[1:]:
        for name in shared_evaluation_fields:
            if getattr(candidate.evaluation, name) != getattr(
                reference_evaluation, name
            ):
                raise ValueError(f"候補間の評価証跡が不一致です: {name}")
    eligible = [candidate for candidate in candidates if candidate.gate_passed]
    if not eligible:
        raise ValueError(
            "validation精度とRaspberry Pi benchmark gateを通過した候補がありません"
        )
    fastest = min(candidate.benchmark.p95_latency_ms for candidate in eligible)
    tied = [
        candidate
        for candidate in eligible
        if fastest == 0.0
        and candidate.benchmark.p95_latency_ms == 0.0
        or fastest > 0.0
        and (candidate.benchmark.p95_latency_ms - fastest) / fastest < 0.05
    ]
    selected = min(
        tied,
        key=lambda candidate: (
            candidate.dependency_complexity_rank,
            candidate.artifact_size_bytes,
            candidate.candidate_id,
        ),
    )
    return SelectedModelCandidate(
        candidate=selected,
        compared_candidate_ids=tuple(sorted(ids)),
    )


def evaluate_frozen_test_candidate(
    selection: SelectedModelCandidate,
    *,
    predictions: Sequence[CandidatePrediction],
    evaluation_dataset_fingerprint: str | None = None,
    evaluation_split_fingerprint: str | None = None,
) -> CandidateEvaluation:
    """候補固定後だけ、validationで決めたthresholdを凍結testへ適用する."""

    validation = selection.candidate.evaluation
    metadata = _validate_candidate_artifact(
        selection.candidate.model_path, validation.model_format
    )
    actual_hash = _sha256_file(selection.candidate.model_path)
    if actual_hash != validation.model_artifact_sha256:
        raise ValueError("選択後にmodel artifactが変更されています")
    _validate_source_export_hash(
        metadata,
        candidate_artifact_sha256=actual_hash,
        fp32_reference_artifact_sha256=(validation.fp32_reference_artifact_sha256),
    )
    lineage = ArtifactLineage(
        source_run_id=validation.source_run_id,
        source_checkpoint_sha256=validation.source_checkpoint_sha256,
        source_checkpoint_role=validation.source_checkpoint_role,
        dataset_fingerprint=validation.training_dataset_fingerprint,
        split_fingerprint=validation.training_split_fingerprint,
        training_protocol_fingerprint=validation.training_protocol_fingerprint,
        parent_run_id=validation.parent_run_id,
        parent_checkpoint_id=validation.parent_checkpoint_id,
    )
    return _evaluate_predictions(
        phase="frozen_test",
        candidate_id=validation.candidate_id,
        model_format=validation.model_format,
        model_artifact_sha256=actual_hash,
        predictions=predictions,
        lineage=lineage,
        evaluation_dataset_fingerprint=(
            validation.dataset_fingerprint
            if evaluation_dataset_fingerprint is None
            else evaluation_dataset_fingerprint
        ),
        evaluation_split_fingerprint=(
            validation.split_fingerprint
            if evaluation_split_fingerprint is None
            else evaluation_split_fingerprint
        ),
        training_sample_ids=validation.training_sample_ids,
        fp32_reference_artifact_sha256=(validation.fp32_reference_artifact_sha256),
        fixed_threshold=validation.uncertainty_relative_std_threshold,
    )


def finalize_selected_model_candidate(
    selection: SelectedModelCandidate,
    frozen_test: CandidateEvaluation,
) -> FinalizedModelCandidate:
    """選択済みartifactとfrozen testをbindし、promotion可能状態にする."""

    validation = selection.candidate.evaluation
    if frozen_test.phase != "frozen_test":
        raise ValueError("frozen test evaluationが必要です")
    for name in (
        "candidate_id",
        "model_format",
        "model_artifact_sha256",
        "fp32_reference_artifact_sha256",
        "training_dataset_fingerprint",
        "training_split_fingerprint",
        "training_protocol_fingerprint",
        "source_run_id",
        "source_checkpoint_sha256",
        "source_checkpoint_role",
        "parent_run_id",
        "parent_checkpoint_id",
    ):
        if getattr(frozen_test, name) != getattr(validation, name):
            raise ValueError(f"frozen testが選択artifactと不一致です: {name}")
    if frozen_test.training_sample_ids != validation.training_sample_ids:
        raise ValueError("frozen testのtraining sample証跡が不一致です")
    validation_ids = set(validation.evaluated_sample_ids)
    frozen_ids = set(frozen_test.evaluated_sample_ids)
    if validation_ids & frozen_ids:
        raise ValueError("frozen testがvalidation sampleを再利用しています")
    if set(frozen_test.training_sample_ids) & frozen_ids:
        raise ValueError("frozen testがtraining sampleを再利用しています")
    if frozen_test.uncertainty_relative_std_threshold != (
        validation.uncertainty_relative_std_threshold
    ):
        raise ValueError("frozen testでuncertainty thresholdを再選択できません")
    if not frozen_test.gate_passed:
        raise ValueError(
            f"frozen test gateを通過していません: {frozen_test.gate_failures}"
        )
    return FinalizedModelCandidate(selection=selection, frozen_test=frozen_test)


def package_model_candidate(
    evaluation: CandidateEvaluation,
    model_path: Path,
    output_directory: Path,
    *,
    preprocess_schema: Mapping[str, Any],
    training_coverage: TrainingCoverage,
    model_name: str,
    model_version: str,
) -> ModelPackageResult:
    """手入力coverageではrelease packageを作れないことを明示する."""

    raise ValueError(
        "手入力のtraining coverageではpackageを作成できません。"
        "package_model_candidate_from_datasetを使用してください"
    )


def _package_model_candidate_verified(
    candidate: ModelCandidateValidation,
    output_directory: Path,
    *,
    preprocess_schema: Mapping[str, Any],
    training_coverage: TrainingCoverage,
    model_name: str,
    model_version: str,
) -> ModelPackageResult:
    """Persisted dataset証跡を検証済みの正式入口からだけ呼ぶ."""

    evaluation = candidate.evaluation
    if evaluation.phase != "validation":
        raise ValueError("candidate packageにはvalidation evaluationが必要です")
    _validate_evaluation_gate_numbers(evaluation)
    lineage = _evaluation_lineage(evaluation)
    return _write_model_package(
        status="candidate",
        candidate_id=evaluation.candidate_id,
        model_format=evaluation.model_format,
        model_path=candidate.model_path,
        output_directory=output_directory,
        preprocess_schema=preprocess_schema,
        training_coverage=training_coverage,
        model_name=model_name,
        model_version=model_version,
        uncertainty_threshold=evaluation.uncertainty_relative_std_threshold,
        lineage=lineage,
        evaluation_payload={
            "schema_version": EVALUATION_SCHEMA_VERSION,
            "validation": evaluation.to_dict(),
            "compile_parity": candidate.compile_parity.to_dict(),
            "export_parity": candidate.export_parity.to_dict(),
        },
        promotion_payload=None,
    )


def package_model_candidate_from_dataset(
    candidate: ModelCandidateValidation,
    output_directory: Path,
    *,
    dataset_paths: Sequence[Path],
    split_manifest: Path,
    model_name: str,
    model_version: str,
) -> ModelPackageResult:
    """実datasetのtrain assignmentからcoverageを導出して候補packageを作る."""

    evaluation = candidate.evaluation
    lineage = _evaluation_lineage(evaluation)
    composite, samples, split = _resolve_evaluation_dataset(
        dataset_paths,
        split_manifest,
        lineage=lineage,
        require_training_lineage_match=True,
    )
    _validate_persisted_evaluation_assignment(
        evaluation,
        dataset_fingerprint=composite.composite_fingerprint,
        split_fingerprint=split.split_fingerprint,
        evaluated_sample_ids=split.validation_sample_ids,
        training_sample_ids=split.train_sample_ids,
    )
    coverage = _training_coverage_from_resolved_dataset(samples, split)
    return _package_model_candidate_verified(
        candidate,
        output_directory,
        preprocess_schema=read_onnx_preprocess_schema(candidate.model_path),
        training_coverage=coverage,
        model_name=model_name,
        model_version=model_version,
    )


def promote_model_package(
    finalized: FinalizedModelCandidate,
    output_directory: Path,
    *,
    preprocess_schema: Mapping[str, Any],
    training_coverage: TrainingCoverage,
    model_name: str,
    model_version: str,
) -> ModelPackageResult:
    """手入力coverageではproduction promotionできないことを明示する."""

    raise ValueError(
        "手入力のtraining coverageではpromotionできません。"
        "promote_model_package_from_datasetを使用してください"
    )


def _promote_model_package_verified(
    finalized: FinalizedModelCandidate,
    output_directory: Path,
    *,
    cross_validation_evidence: Sequence[CrossValidationPromotionEvidence],
    training_dataset_sample_ids: Sequence[str],
    preprocess_schema: Mapping[str, Any],
    training_coverage: TrainingCoverage,
    model_name: str,
    model_version: str,
) -> ModelPackageResult:
    """Persisted dataset証跡を検証済みの正式入口からだけ呼ぶ."""

    validation = finalized.selection.candidate.evaluation
    benchmark = finalized.selection.candidate.benchmark
    frozen_test = finalized.frozen_test
    if not validation.gate_passed:
        raise ValueError("validation gateを通過していません")
    _validate_evaluation_gate_numbers(validation)
    _validate_evaluation_gate_numbers(frozen_test)
    if not benchmark.gate_passed:
        raise ValueError("Raspberry Pi 5 p95 benchmark gateを通過していません")
    if benchmark.schema_version != BENCHMARK_SCHEMA_VERSION:
        raise ValueError("未知のbenchmark schemaです")
    _validate_benchmark_iterations(benchmark)
    model_path = finalized.selection.candidate.model_path
    actual_hash = _sha256_file(model_path)
    if actual_hash != validation.model_artifact_sha256:
        raise ValueError("promotion前にmodel artifactが変更されています")
    lineage = _evaluation_lineage(validation)
    validated_cross_validation = _validate_cross_validation_release_evidence(
        cross_validation_evidence,
        lineage=lineage,
        dataset_sample_ids=training_dataset_sample_ids,
    )
    metadata = _read_onnx_export_metadata(model_path)
    if _lineage_from_mapping(_required_mapping(metadata, "lineage")) != lineage:
        raise ValueError("promotion modelのembedded lineageが不一致です")
    embedded_preprocess = _required_mapping(metadata, "preprocess_schema")
    if _canonical_json(embedded_preprocess) != _canonical_json(preprocess_schema):
        raise ValueError("promotion modelのpreprocess schemaが不一致です")
    return _write_model_package(
        status="promoted",
        candidate_id=validation.candidate_id,
        model_format=validation.model_format,
        model_path=model_path,
        output_directory=output_directory,
        preprocess_schema=preprocess_schema,
        training_coverage=training_coverage,
        model_name=model_name,
        model_version=model_version,
        uncertainty_threshold=validation.uncertainty_relative_std_threshold,
        lineage=lineage,
        evaluation_payload={
            "schema_version": EVALUATION_SCHEMA_VERSION,
            "validation": validation.to_dict(),
            "frozen_test": frozen_test.to_dict(),
            "benchmark": benchmark.to_dict(),
            "compile_parity": (finalized.selection.candidate.compile_parity.to_dict()),
            "export_parity": (finalized.selection.candidate.export_parity.to_dict()),
            "cross_validation": [
                evidence.to_dict() for evidence in validated_cross_validation
            ],
        },
        promotion_payload={
            "selected_candidate_id": validation.candidate_id,
            "compared_candidate_ids": list(finalized.selection.compared_candidate_ids),
            "gate_spec": _gate_spec(),
        },
    )


def promote_model_package_from_dataset(
    finalized: FinalizedModelCandidate,
    output_directory: Path,
    *,
    dataset_paths: Sequence[Path],
    split_manifest: Path,
    cross_validation_evidence: Sequence[CrossValidationPromotionEvidence],
    frozen_test_dataset_paths: Sequence[Path] | None = None,
    frozen_test_split_manifest: Path | None = None,
    model_name: str,
    model_version: str,
) -> ModelPackageResult:
    """Persisted splitの全release証跡とcoverageを検証してpromoteする正式入口."""

    validation = finalized.selection.candidate.evaluation
    lineage = _evaluation_lineage(validation)
    composite, samples, split = _resolve_evaluation_dataset(
        dataset_paths,
        split_manifest,
        lineage=lineage,
        require_training_lineage_match=True,
    )
    _validate_persisted_evaluation_assignment(
        validation,
        dataset_fingerprint=composite.composite_fingerprint,
        split_fingerprint=split.split_fingerprint,
        evaluated_sample_ids=split.validation_sample_ids,
        training_sample_ids=split.train_sample_ids,
    )
    if (frozen_test_dataset_paths is None) != (frozen_test_split_manifest is None):
        raise ValueError(
            "external frozen testのdataset_pathsとsplit_manifestは両方指定してください"
        )
    if frozen_test_dataset_paths is None:
        frozen_composite = composite
        frozen_split = split
    else:
        frozen_composite, _, frozen_split = _resolve_evaluation_dataset(
            frozen_test_dataset_paths,
            cast(Path, frozen_test_split_manifest),
            lineage=lineage,
            require_training_lineage_match=False,
        )
    _validate_persisted_evaluation_assignment(
        finalized.frozen_test,
        dataset_fingerprint=frozen_composite.composite_fingerprint,
        split_fingerprint=frozen_split.split_fingerprint,
        evaluated_sample_ids=frozen_split.test_sample_ids,
        training_sample_ids=split.train_sample_ids,
    )
    coverage = _training_coverage_from_resolved_dataset(samples, split)
    model_path = finalized.selection.candidate.model_path
    return _promote_model_package_verified(
        finalized,
        output_directory,
        cross_validation_evidence=cross_validation_evidence,
        training_dataset_sample_ids=tuple(
            sorted(sample.sample_id for sample in samples)
        ),
        preprocess_schema=read_onnx_preprocess_schema(model_path),
        training_coverage=coverage,
        model_name=model_name,
        model_version=model_version,
    )


def build_training_coverage_from_dataset(
    dataset_paths: Sequence[Path],
    split_manifest: Path,
    *,
    lineage: ArtifactLineage,
) -> TrainingCoverage:
    """Persisted train splitだけからproduction coverage範囲を作る."""

    _, samples, split = _resolve_evaluation_dataset(
        dataset_paths,
        split_manifest,
        lineage=lineage,
        require_training_lineage_match=True,
    )
    return _training_coverage_from_resolved_dataset(samples, split)


def _training_coverage_from_resolved_dataset(
    samples: Sequence[Any], split: Any
) -> TrainingCoverage:
    by_id = {sample.sample_id: sample for sample in samples}
    training_samples = [by_id[sample_id] for sample_id in split.train_sample_ids]
    if not training_samples:
        raise ValueError("train splitにcoverage sampleがありません")
    return TrainingCoverage(
        pixel_per_mm_min=min(sample.pixel_per_mm for sample in training_samples),
        pixel_per_mm_max=max(sample.pixel_per_mm for sample in training_samples),
        height_min=min(sample.height for sample in training_samples),
        height_max=max(sample.height for sample in training_samples),
        width_min=min(sample.width for sample in training_samples),
        width_max=max(sample.width for sample in training_samples),
    )


def _validate_persisted_evaluation_assignment(
    evaluation: CandidateEvaluation,
    *,
    dataset_fingerprint: str,
    split_fingerprint: str,
    evaluated_sample_ids: Sequence[str],
    training_sample_ids: Sequence[str],
) -> None:
    expected_evaluated_ids = tuple(evaluated_sample_ids)
    if not expected_evaluated_ids:
        raise ValueError(f"persisted {evaluation.phase} splitにsampleがありません")
    checks = (
        ("dataset_fingerprint", evaluation.dataset_fingerprint, dataset_fingerprint),
        ("split_fingerprint", evaluation.split_fingerprint, split_fingerprint),
        (
            "evaluated_sample_ids",
            evaluation.evaluated_sample_ids,
            expected_evaluated_ids,
        ),
        (
            "training_sample_ids",
            evaluation.training_sample_ids,
            tuple(training_sample_ids),
        ),
    )
    for name, actual, expected in checks:
        if actual != expected:
            raise ValueError(
                f"{evaluation.phase} reportがpersisted dataset証跡と不一致です: {name}"
            )


def _validate_cross_validation_release_evidence(
    evidence_items: Sequence[CrossValidationPromotionEvidence],
    *,
    lineage: ArtifactLineage,
    dataset_sample_ids: Sequence[str],
) -> tuple[CrossValidationPromotionEvidence, ...]:
    """Persisted strict reportsをtraining lineageにbindする."""

    expected_dimensions = ("machine", "paste_lot", "nozzle")
    if len(evidence_items) != len(expected_dimensions) or any(
        not isinstance(evidence, CrossValidationPromotionEvidence)
        for evidence in evidence_items
    ):
        raise ValueError(
            "promotionにはmachine/paste_lot/nozzleの3 typed cross-validation evidenceが必要です"
        )
    by_dimension = {evidence.report.dimension: evidence for evidence in evidence_items}
    if set(by_dimension) != set(expected_dimensions) or len(by_dimension) != len(
        expected_dimensions
    ):
        raise ValueError(
            "cross-validation dimensionはmachine/paste_lot/nozzleを1件ずつ指定してください"
        )
    validated: list[CrossValidationPromotionEvidence] = []
    expected_dataset_sample_ids = tuple(sorted(dataset_sample_ids))
    if not expected_dataset_sample_ids:
        raise ValueError("cross-validation bind対象datasetにsampleがありません")
    for dimension in expected_dimensions:
        evidence = by_dimension[dimension]
        report = evidence.report
        attestation = evidence.summary_attestation
        if not report.available or report.reason is not None:
            raise ValueError(
                f"releaseにはavailable=trueのcross-validationが必要です: {dimension}"
            )
        if report.composite_fingerprint != lineage.dataset_fingerprint:
            raise ValueError(
                f"cross-validation dataset lineageが不一致です: {dimension}"
            )
        if report.protocol_fingerprint != lineage.training_protocol_fingerprint:
            raise ValueError(
                f"cross-validation training protocolが不一致です: {dimension}"
            )
        if report.dataset_sample_ids != expected_dataset_sample_ids:
            raise ValueError(
                f"cross-validation dataset sample IDsが不一致です: {dimension}"
            )
        persisted_path = Path(report.report_path).expanduser().resolve(strict=True)
        if (
            attestation.output_path != persisted_path
            or attestation.output_kind != "file"
            or attestation.run_kind != "cross-validation-summary"
            or attestation.output_fingerprint
            != "sha256:" + _sha256_file(persisted_path)
        ):
            raise ValueError(
                f"cross-validation formal attestationがreportと不一致です: {dimension}"
            )
        persisted = load_cross_validation_result(persisted_path)
        if persisted.to_dict() != report.to_dict():
            raise ValueError(
                f"cross-validation persisted reportがtyped evidenceと不一致です: {dimension}"
            )
        validated.append(evidence)
    run_ids = tuple(item.summary_attestation.run_id for item in validated)
    if len(set(run_ids)) != len(expected_dimensions):
        raise ValueError("cross-validation summary run IDが重複しています")
    tracking_uri_hashes = {
        item.summary_attestation.tracking_uri_sha256 for item in validated
    }
    if len(tracking_uri_hashes) != 1:
        raise ValueError("cross-validation tracking URIが一致しません")
    return tuple(validated)


def promote_and_activate_model_package(
    finalized: FinalizedModelCandidate,
    output_directory: Path,
    active_pointer: Path,
    *,
    preprocess_schema: Mapping[str, Any],
    training_coverage: TrainingCoverage,
    model_name: str,
    model_version: str,
) -> tuple[ModelPackageResult, ActiveModelPointer]:
    """手入力coverageではpromote+activateできないことを明示する."""

    raise ValueError(
        "手入力のtraining coverageではpromote+activateできません。"
        "promote_and_activate_model_package_from_datasetを使用してください"
    )


def promote_and_activate_model_package_from_dataset(
    finalized: FinalizedModelCandidate,
    output_directory: Path,
    active_pointer: Path,
    *,
    dataset_paths: Sequence[Path],
    split_manifest: Path,
    cross_validation_evidence: Sequence[CrossValidationPromotionEvidence],
    frozen_test_dataset_paths: Sequence[Path] | None = None,
    frozen_test_split_manifest: Path | None = None,
    model_name: str,
    model_version: str,
) -> tuple[ModelPackageResult, ActiveModelPointer]:
    """Persisted dataset証跡を検証後にpromoteし、active pointerを切り替える."""

    package = promote_model_package_from_dataset(
        finalized,
        output_directory,
        dataset_paths=dataset_paths,
        split_manifest=split_manifest,
        cross_validation_evidence=cross_validation_evidence,
        frozen_test_dataset_paths=frozen_test_dataset_paths,
        frozen_test_split_manifest=frozen_test_split_manifest,
        model_name=model_name,
        model_version=model_version,
    )
    pointer = activate_model_package(package.package_path, active_pointer)
    return package, pointer


def activate_model_package(
    model_package: Path, pointer_file: Path
) -> ActiveModelPointer:
    """検証済みpromoted packageへactive pointerをatomic switchする."""

    from .inference import verify_model_package

    package_path = Path(model_package).expanduser().resolve()
    pointer_path = Path(pointer_file).expanduser().resolve()
    if pointer_path.is_relative_to(package_path):
        raise ValueError(
            "active model pointerはimmutable model packageの外に配置してください"
        )
    verified = verify_model_package(package_path, require_promoted=True)
    model = _required_mapping(verified.manifest, "model")
    model_id = _required_string(model, "id")
    previous: Path | None = None
    if pointer_path.exists():
        current = _load_active_pointer(pointer_path)
        previous = current.active_model_path
        if previous == verified.path:
            previous = current.previous_model_path
    value = {
        "schema_version": ACTIVE_MODEL_POINTER_SCHEMA_VERSION,
        "active_model_path": str(verified.path),
        "previous_model_path": None if previous is None else str(previous),
        "active_model_id": model_id,
        "active_model_sha256": verified.model_sha256,
        "updated_at_unix_ns": time.time_ns(),
    }
    _atomic_json(pointer_path, value)
    return ActiveModelPointer(
        pointer_path=pointer_path,
        active_model_path=verified.path,
        previous_model_path=previous,
        active_model_id=model_id,
        active_model_sha256=verified.model_sha256,
    )


def rollback_active_model(pointer_file: Path) -> ActiveModelPointer:
    """保持中の直前packageを再検証し、active/previousを手動で入れ替える."""

    from .inference import verify_model_package

    pointer_path = Path(pointer_file).expanduser().resolve(strict=True)
    current = _load_active_pointer(pointer_path)
    if current.previous_model_path is None:
        raise ValueError("rollback対象のprevious modelがありません")
    verified = verify_model_package(current.previous_model_path, require_promoted=True)
    model = _required_mapping(verified.manifest, "model")
    model_id = _required_string(model, "id")
    value = {
        "schema_version": ACTIVE_MODEL_POINTER_SCHEMA_VERSION,
        "active_model_path": str(verified.path),
        "previous_model_path": str(current.active_model_path),
        "active_model_id": model_id,
        "active_model_sha256": verified.model_sha256,
        "updated_at_unix_ns": time.time_ns(),
    }
    _atomic_json(pointer_path, value)
    return ActiveModelPointer(
        pointer_path=pointer_path,
        active_model_path=verified.path,
        previous_model_path=current.active_model_path,
        active_model_id=model_id,
        active_model_sha256=verified.model_sha256,
    )


def _release_evaluation_summary(
    evaluation_payload: Mapping[str, Any],
) -> dict[str, object]:
    summary: dict[str, object] = {}
    for phase_name in ("validation", "frozen_test"):
        raw = evaluation_payload.get(phase_name)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise ValueError(f"{phase_name} evaluation payloadが不正です")
        phase = cast(Mapping[str, Any], raw)
        sample_ids = phase.get("evaluated_sample_ids")
        if not isinstance(sample_ids, list) or not all(
            isinstance(item, str) and item for item in sample_ids
        ):
            raise ValueError(f"{phase_name} evaluated_sample_idsが不正です")
        summary[phase_name] = {
            "dataset_fingerprint": _required_string(phase, "dataset_fingerprint"),
            "split_fingerprint": _required_string(phase, "split_fingerprint"),
            "sample_count": len(sample_ids),
            "sample_ids_sha256": _canonical_json_sha256(
                {"sample_ids": sorted(sample_ids)}
            ),
        }
    if "validation" not in summary:
        raise ValueError("validation release evaluation証跡が必要です")
    return summary


def _write_model_package(
    *,
    status: Literal["candidate", "promoted"],
    candidate_id: str,
    model_format: ModelFormat,
    model_path: Path,
    output_directory: Path,
    preprocess_schema: Mapping[str, Any],
    training_coverage: TrainingCoverage,
    model_name: str,
    model_version: str,
    uncertainty_threshold: float,
    lineage: ArtifactLineage,
    evaluation_payload: Mapping[str, Any],
    promotion_payload: Mapping[str, Any] | None,
) -> ModelPackageResult:
    _validate_canonical_preprocess_schema(preprocess_schema)
    if not model_name or not model_version:
        raise ValueError("model_name/model_versionを空にできません")
    if not _is_positive_finite(uncertainty_threshold):
        raise ValueError("uncertainty thresholdは正の有限値が必要です")
    source = Path(model_path).expanduser().resolve(strict=True)
    _validate_candidate_artifact(source, model_format)
    artifact_hash = _sha256_file(source)
    if candidate_id != _candidate_id(model_format, artifact_hash):
        raise ValueError("candidate_idがmodel artifact fingerprintと不一致です")
    metadata = _read_onnx_export_metadata(source)
    uncertainty_offset = metadata.get("uncertainty_log_variance_offset")
    if not _is_finite_number(uncertainty_offset):
        raise ValueError("modelのuncertainty calibration offsetが不正です")
    export_provenance = _required_mapping(metadata, "export_provenance")
    artifact_provenance = _required_mapping(metadata, "artifact_provenance")
    validation_payload = _required_mapping(evaluation_payload, "validation")
    compile_parity = compile_parity_report_from_dict(
        _required_mapping(evaluation_payload, "compile_parity")
    )
    export_parity = export_parity_report_from_dict(
        _required_mapping(evaluation_payload, "export_parity")
    )
    _validate_release_compile_parity(compile_parity)
    if not export_parity.success:
        raise ValueError("export parity gateを通過していません")
    _validate_source_export_hash(
        metadata,
        candidate_artifact_sha256=artifact_hash,
        fp32_reference_artifact_sha256=_required_string(
            validation_payload, "fp32_reference_artifact_sha256"
        ),
    )
    if _lineage_from_mapping(_required_mapping(metadata, "lineage")) != lineage:
        raise ValueError("package modelのembedded lineageが不一致です")
    training_weights_attestation = _formal_training_attestation_from_metadata(
        metadata, lineage
    )
    if compile_parity.provenance.weights_sha256 != metadata.get(
        "source_weights_sha256"
    ):
        raise ValueError("package compile parityのsource weightsが不一致です")
    if export_parity.weights_sha256 != metadata.get("source_weights_sha256"):
        raise ValueError("package export parityのsource weightsが不一致です")
    if export_parity.fp32_model_artifact_sha256 != _required_string(
        validation_payload, "fp32_reference_artifact_sha256"
    ):
        raise ValueError("package export parityのFP32 sourceが不一致です")
    if (
        compile_parity.provenance.training_protocol_fingerprint
        != lineage.training_protocol_fingerprint
        or compile_parity.provenance.dataset_fingerprint != lineage.dataset_fingerprint
        or compile_parity.provenance.split_fingerprint != lineage.split_fingerprint
    ):
        raise ValueError("package compile parityのtraining lineageが不一致です")
    if (
        export_parity.training_protocol_fingerprint
        != lineage.training_protocol_fingerprint
        or export_parity.dataset_fingerprint
        != _required_string(validation_payload, "dataset_fingerprint")
        or export_parity.split_fingerprint
        != _required_string(validation_payload, "split_fingerprint")
        or list(export_parity.sample_ids)
        != validation_payload.get("evaluated_sample_ids")
        or list(export_parity.training_sample_ids)
        != validation_payload.get("training_sample_ids")
    ):
        raise ValueError("package export parityのvalidation assignmentが不一致です")
    cross_validation_evidence: list[object] | None = None
    cross_validation_summary: list[dict[str, object]] | None = None
    if status == "promoted":
        cross_raw = evaluation_payload.get("cross_validation")
        if not isinstance(cross_raw, list) or len(cross_raw) != 3:
            raise ValueError(
                "promoted packageには3 cross-validation evidenceが必要です"
            )
        cross_validation_evidence = list(cross_raw)
        cross_validation_summary = []
        for raw_evidence in cross_validation_evidence:
            if not isinstance(raw_evidence, Mapping):
                raise ValueError("cross-validation promotion evidenceが不正です")
            report = _required_mapping(raw_evidence, "report")
            attestation = _required_mapping(raw_evidence, "summary_attestation")
            cross_validation_summary.append(
                {
                    "dimension": _required_string(report, "dimension"),
                    "report_fingerprint": _required_string(
                        report, "report_fingerprint"
                    ),
                    "report_artifact_sha256": _required_string(
                        attestation, "output_fingerprint"
                    ),
                    "summary_run_id": _required_string(attestation, "run_id"),
                    "tracking_uri_sha256": _required_string(
                        attestation, "tracking_uri_sha256"
                    ),
                    "attestation_sha256": "sha256:"
                    + _canonical_json_sha256(attestation),
                }
            )
    elif "cross_validation" in evaluation_payload:
        raise ValueError(
            "candidate packageにcross-validation promotion evidenceを含められません"
        )
    if _canonical_json(
        _required_mapping(metadata, "preprocess_schema")
    ) != _canonical_json(preprocess_schema):
        raise ValueError("package modelのembedded preprocess schemaが不一致です")
    import onnx
    import onnxruntime as ort

    onnx_model = onnx.load(str(source), load_external_data=False)
    opset_versions = [
        item.version
        for item in onnx_model.opset_import
        if item.domain in {"", "ai.onnx"}
    ]
    if not opset_versions:
        raise ValueError("ONNX modelのstandard opsetがありません")
    destination = Path(output_directory).expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"model packageが既に存在します: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    try:
        shutil.copyfile(source, temporary / "model.onnx")
        _write_json(temporary / "preprocess.json", preprocess_schema)
        _write_json(temporary / "evaluation.json", evaluation_payload)
        file_hashes = {
            filename: _sha256_file(temporary / filename)
            for filename in ("model.onnx", "preprocess.json", "evaluation.json")
        }
        manifest: dict[str, Any] = {
            "schema_version": MODEL_PACKAGE_SCHEMA_VERSION,
            "status": status,
            "model": {
                "id": candidate_id,
                "name": model_name,
                "version": model_version,
                "format": model_format,
                "artifact_sha256": file_hashes["model.onnx"],
            },
            "runtime": {
                "name": "onnxruntime",
                "minimum_version": _major_minor_version(ort.__version__),
                "exporter": "torch.onnx.export(dynamo=True)",
                "opset_version": max(opset_versions),
                "quantization": (
                    "none" if model_format == "onnx-fp32" else "static-int8-qdq"
                ),
            },
            "uncertainty_calibration": {
                "kind": "log-variance-offset",
                "log_variance_offset": float(uncertainty_offset),
            },
            "provenance": {
                "export": dict(export_provenance),
                "artifact": dict(artifact_provenance),
            },
            "input_contract": {
                "batch_size": 1,
                "dynamic_height_width": True,
                "inputs": {
                    "image_6ch": "image_6ch",
                    "valid_pixel_mask": "valid_pixel_mask",
                    "pixel_per_mm": "pixel_per_mm",
                },
                "outputs": {
                    "mean_volume_ul": "mean_volume_ul",
                    "log_variance_volume_ul2": "log_variance_volume_ul2",
                },
            },
            "preprocess_schema_sha256": _canonical_json_sha256(preprocess_schema),
            "training_coverage": training_coverage.to_dict(),
            "uncertainty_relative_std_threshold": uncertainty_threshold,
            "lineage": lineage.to_dict(),
            "training_weights_attestation": training_weights_attestation.to_dict(),
            "compile_parity": compile_parity.to_dict(),
            "export_parity": export_parity.to_dict(),
            "release_evaluation": _release_evaluation_summary(evaluation_payload),
            "files": file_hashes,
        }
        if cross_validation_summary is not None:
            manifest["cross_validation"] = cross_validation_summary
        if promotion_payload is not None:
            manifest["promotion"] = dict(promotion_payload)
        _write_json(temporary / "manifest.json", manifest)
        sums = {
            filename: _sha256_file(temporary / filename)
            for filename in (
                "manifest.json",
                "model.onnx",
                "preprocess.json",
                "evaluation.json",
            )
        }
        (temporary / "SHA256SUMS").write_text(
            "".join(
                f"{digest}  {filename}\n" for filename, digest in sorted(sums.items())
            ),
            encoding="utf-8",
        )
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    from .inference import verify_model_package

    try:
        verified = verify_model_package(
            destination, require_promoted=status == "promoted"
        )
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return ModelPackageResult(
        status=status,
        package_path=destination,
        model_id=candidate_id,
        model_artifact_sha256=verified.model_sha256,
        manifest_sha256=_sha256_file(destination / "manifest.json"),
        lineage=lineage,
    )


def _load_active_pointer(path: Path) -> ActiveModelPointer:
    raw = _read_json(path)
    if raw.get("schema_version") != ACTIVE_MODEL_POINTER_SCHEMA_VERSION:
        raise ValueError("未知のactive model pointer schemaです")
    active_path = Path(_required_string(raw, "active_model_path"))
    previous_raw = raw.get("previous_model_path")
    if previous_raw is not None and not isinstance(previous_raw, str):
        raise ValueError("previous_model_pathが不正です")
    previous = None if previous_raw is None else Path(previous_raw).resolve()
    checksum = _required_string(raw, "active_model_sha256")
    if not _is_sha256(checksum):
        raise ValueError("active model checksumが不正です")
    return ActiveModelPointer(
        pointer_path=path,
        active_model_path=active_path.resolve(),
        previous_model_path=previous,
        active_model_id=_required_string(raw, "active_model_id"),
        active_model_sha256=checksum,
    )


def _evaluate_predictions(
    *,
    phase: Literal["validation", "frozen_test"],
    candidate_id: str,
    model_format: ModelFormat,
    model_artifact_sha256: str,
    predictions: Sequence[CandidatePrediction],
    lineage: ArtifactLineage,
    evaluation_dataset_fingerprint: str,
    evaluation_split_fingerprint: str,
    training_sample_ids: Sequence[str],
    fp32_reference_artifact_sha256: str,
    fixed_threshold: float | None,
) -> CandidateEvaluation:
    if model_format not in ("onnx-fp32", "onnx-int8-qdq"):
        raise ValueError(f"未知のmodel formatです: {model_format}")
    if not predictions:
        raise ValueError("evaluation predictionがありません")
    if not evaluation_dataset_fingerprint or not evaluation_split_fingerprint:
        raise ValueError("evaluation dataset/split fingerprintが必要です")
    checked_training_sample_ids = tuple(training_sample_ids)
    if not checked_training_sample_ids:
        raise ValueError("training sample証跡が必要です")
    if any(not sample_id for sample_id in checked_training_sample_ids):
        raise ValueError("training sample_idを空にできません")
    if len(set(checked_training_sample_ids)) != len(checked_training_sample_ids):
        raise ValueError("training sample_idが重複しています")
    if len({item.sample_id for item in predictions}) != len(predictions):
        raise ValueError("evaluation sample_idが重複しています")
    evaluated_sample_ids = tuple(item.sample_id for item in predictions)
    if set(evaluated_sample_ids) & set(checked_training_sample_ids):
        raise ValueError("evaluationがtraining sampleを再利用しています")
    for item in predictions:
        values = (
            item.mean_volume_ul,
            item.log_variance_volume_ul2,
            item.reference_mean_volume_ul,
            item.reference_log_variance_volume_ul2,
            *item.padded_mean_volume_ul.values(),
        )
        if not all(_is_finite_number(value) for value in values):
            raise ValueError(f"predictionに非有限値があります: {item.sample_id}")
        if item.mean_volume_ul <= 0 or item.reference_mean_volume_ul <= 0:
            raise ValueError(f"prediction meanは正値が必要です: {item.sample_id}")

    target = [item.target_volume_ul for item in predictions]
    means = [item.mean_volume_ul for item in predictions]
    log_variances = [item.log_variance_volume_ul2 for item in predictions]
    reference_means = [item.reference_mean_volume_ul for item in predictions]
    reference_log_variances = [
        item.reference_log_variance_volume_ul2 for item in predictions
    ]
    primary_score = _accuracy_score(target, means)
    coverage = _normal_coverage(target, means, log_variances)
    reference_score = _accuracy_score(target, reference_means)
    reference_coverage = _normal_coverage(
        target, reference_means, reference_log_variances
    )
    mean_errors = [
        abs(item.mean_volume_ul - item.reference_mean_volume_ul) for item in predictions
    ]
    log_variance_errors = [
        abs(item.log_variance_volume_ul2 - item.reference_log_variance_volume_ul2)
        for item in predictions
    ]
    mean_tolerances = [
        max(
            FP32_ABSOLUTE_TOLERANCE_UL,
            abs(item.reference_mean_volume_ul) * FP32_RELATIVE_TOLERANCE,
        )
        for item in predictions
    ]
    log_variance_tolerances = [
        max(
            FP32_ABSOLUTE_TOLERANCE_UL,
            abs(item.reference_log_variance_volume_ul2) * FP32_RELATIVE_TOLERANCE,
        )
        for item in predictions
    ]
    parity_passed = all(
        mean_error <= mean_tolerance and log_variance_error <= log_variance_tolerance
        for mean_error, mean_tolerance, log_variance_error, log_variance_tolerance in zip(
            mean_errors,
            mean_tolerances,
            log_variance_errors,
            log_variance_tolerances,
            strict=True,
        )
    )
    padding_degradation = _padding_degradation(predictions, primary_score)
    relative_std = [
        math.exp(0.5 * item.log_variance_volume_ul2) / item.mean_volume_ul
        for item in predictions
    ]
    if fixed_threshold is None:
        threshold = _select_uncertainty_threshold(target, means, relative_std)
    else:
        if not _is_positive_finite(fixed_threshold):
            raise ValueError("fixed uncertainty thresholdは正の有限値が必要です")
        threshold = float(fixed_threshold)
    accepted = [value <= threshold for value in relative_std]
    accepted_target = [
        value for value, keep in zip(target, accepted, strict=True) if keep
    ]
    accepted_means = [
        value for value, keep in zip(means, accepted, strict=True) if keep
    ]
    accepted_score = (
        _accuracy_score(accepted_target, accepted_means)
        if accepted_target
        else math.inf
    )
    accepted_coverage = sum(accepted) / len(accepted)
    three_sample_acceptance = _three_sample_acceptance(predictions, accepted)
    failures: list[str] = []
    if primary_score > PRIMARY_ACCURACY_MAX:
        failures.append("primary_accuracy")
    if padding_degradation > PADDING_DEGRADATION_MAX:
        failures.append("padding_invariance")
    if model_format == "onnx-fp32" and not parity_passed:
        failures.append("fp32_export_parity")
    if model_format == "onnx-int8-qdq":
        if primary_score - reference_score > INT8_ACCURACY_DEGRADATION_MAX:
            failures.append("int8_accuracy_degradation")
        if abs(coverage - reference_coverage) > INT8_COVERAGE_DELTA_MAX:
            failures.append("int8_coverage_delta")
    if not accepted_target or accepted_score > PRIMARY_ACCURACY_MAX:
        failures.append("uncertainty_accepted_accuracy")
    if phase == "frozen_test" and (
        three_sample_acceptance < THREE_SAMPLE_ACCEPTANCE_MIN
    ):
        failures.append("three_sample_acceptance")
    return CandidateEvaluation(
        phase=phase,
        candidate_id=candidate_id,
        model_format=model_format,
        model_artifact_sha256=model_artifact_sha256,
        fp32_reference_artifact_sha256=fp32_reference_artifact_sha256,
        training_dataset_fingerprint=lineage.dataset_fingerprint,
        training_split_fingerprint=lineage.split_fingerprint,
        training_protocol_fingerprint=lineage.training_protocol_fingerprint,
        dataset_fingerprint=evaluation_dataset_fingerprint,
        split_fingerprint=evaluation_split_fingerprint,
        evaluated_sample_ids=evaluated_sample_ids,
        training_sample_ids=checked_training_sample_ids,
        source_run_id=lineage.source_run_id,
        source_checkpoint_sha256=lineage.source_checkpoint_sha256,
        source_checkpoint_role=lineage.source_checkpoint_role,
        parent_run_id=lineage.parent_run_id,
        parent_checkpoint_id=lineage.parent_checkpoint_id,
        sample_count=len(predictions),
        primary_accuracy_score=primary_score,
        coverage_68=coverage,
        fp32_reference_accuracy_score=reference_score,
        fp32_reference_coverage_68=reference_coverage,
        maximum_mean_parity_error_ul=max(mean_errors),
        maximum_mean_parity_tolerance_ul=max(mean_tolerances),
        maximum_mean_parity_ratio=max(
            error / tolerance
            for error, tolerance in zip(mean_errors, mean_tolerances, strict=True)
        ),
        maximum_log_variance_parity_error=max(log_variance_errors),
        maximum_log_variance_parity_tolerance=max(log_variance_tolerances),
        maximum_log_variance_parity_ratio=max(
            error / tolerance
            for error, tolerance in zip(
                log_variance_errors, log_variance_tolerances, strict=True
            )
        ),
        fp32_parity_passed=parity_passed,
        padding_accuracy_degradation=padding_degradation,
        uncertainty_relative_std_threshold=threshold,
        accepted_accuracy_score=accepted_score,
        accepted_sample_coverage=accepted_coverage,
        three_sample_acceptance=three_sample_acceptance,
        gate_passed=not failures,
        gate_failures=tuple(failures),
    )


def _accuracy_score(targets: Sequence[float], predictions: Sequence[float]) -> float:
    if len(targets) != len(predictions) or not targets:
        raise ValueError("accuracy metricには同数の非空target/predictionが必要です")
    errors = [
        (prediction - target) / target
        for target, prediction in zip(targets, predictions, strict=True)
    ]
    return abs(statistics.fmean(errors)) + float(np.std(errors, ddof=0))


def _normal_coverage(
    targets: Sequence[float], means: Sequence[float], log_variances: Sequence[float]
) -> float:
    covered = 0
    for target, mean, log_variance in zip(targets, means, log_variances, strict=True):
        try:
            std = math.exp(0.5 * log_variance)
        except OverflowError:
            std = math.inf
        if math.isfinite(std) and mean - std <= target <= mean + std:
            covered += 1
    return covered / len(targets)


def _select_uncertainty_threshold(
    targets: Sequence[float], means: Sequence[float], relative_std: Sequence[float]
) -> float:
    candidates = sorted({value for value in relative_std if _is_positive_finite(value)})
    chosen: float | None = None
    best_coverage = -1.0
    for threshold in candidates:
        selected = [value <= threshold for value in relative_std]
        selected_targets = [
            value for value, keep in zip(targets, selected, strict=True) if keep
        ]
        selected_means = [
            value for value, keep in zip(means, selected, strict=True) if keep
        ]
        if not selected_targets:
            continue
        if _accuracy_score(selected_targets, selected_means) > PRIMARY_ACCURACY_MAX:
            continue
        coverage = len(selected_targets) / len(targets)
        if coverage > best_coverage or (
            math.isclose(coverage, best_coverage)
            and (chosen is None or threshold < chosen)
        ):
            chosen = threshold
            best_coverage = coverage
    if chosen is None:
        return math.nextafter(0.0, 1.0)
    return chosen


def _padding_degradation(
    predictions: Sequence[CandidatePrediction], base_score: float
) -> float:
    for prediction in predictions:
        actual_labels = set(prediction.padded_mean_volume_ul)
        if actual_labels != _REQUIRED_PADDING_CONDITION_LABELS:
            missing = sorted(_REQUIRED_PADDING_CONDITION_LABELS - actual_labels)
            unknown = sorted(actual_labels - _REQUIRED_PADDING_CONDITION_LABELS)
            raise ValueError(
                "padding condition集合が規定値と一致しません: "
                f"sample_id={prediction.sample_id}, missing={missing}, "
                f"unknown={unknown}"
            )
    maximum = 0.0
    for label in sorted(_REQUIRED_PADDING_CONDITION_LABELS):
        score = _accuracy_score(
            [prediction.target_volume_ul for prediction in predictions],
            [prediction.padded_mean_volume_ul[label] for prediction in predictions],
        )
        maximum = max(maximum, score - base_score)
    return maximum


def _three_sample_acceptance(
    predictions: Sequence[CandidatePrediction], accepted: Sequence[bool]
) -> float:
    if not accepted:
        return 0.0
    grouped: dict[str, list[tuple[int, bool]]] = {}
    for prediction, is_accepted in zip(predictions, accepted, strict=True):
        grouped.setdefault(prediction.sequence_group, []).append(
            (prediction.sequence_index, is_accepted)
        )
    windows: list[list[bool]] = []
    for values in grouped.values():
        ordered = [value for _, value in sorted(values)]
        windows.extend(ordered[index : index + 3] for index in range(len(ordered) - 2))
    if not windows:
        return 0.0
    return sum(any(window) for window in windows) / len(windows)


def _candidate_id(model_format: ModelFormat, artifact_hash: str) -> str:
    return f"{model_format}:{artifact_hash[:16]}"


@attrs.frozen
class _LoadedWeights:
    model: Any
    lineage: ArtifactLineage
    run_kind: Literal["base-train", "finetune"]
    model_config: Mapping[str, Any]
    preprocess_schema: Mapping[str, Any]
    uncertainty_log_variance_offset: float


def export_onnx_model(
    weights_path: Path,
    output_path: Path,
    *,
    opset_version: int = DEFAULT_OPSET_VERSION,
    verification_shapes: Sequence[tuple[int, int]] = (
        (32, 32),
        (64, 128),
        (128, 64),
        (512, 512),
        (256, 1024),
    ),
    _formal_weights: FormalTrainingWeights | None = None,
) -> OnnxExportResult:
    """Low-level diagnostic export; release callers use the formal wrapper."""

    if opset_version < 18:
        raise ValueError("opset_versionは18以上が必要です")
    shapes = _validate_shapes(verification_shapes)
    weights = (
        _load_weights(weights_path)
        if _formal_weights is None
        else _validate_formal_training_weights(_formal_weights, weights_path)
    )
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"ONNX outputが既に存在します: {destination}")

    import torch
    from torch import nn

    class _ExportWrapper(nn.Module):
        def __init__(self, model: nn.Module, offset: float) -> None:
            super().__init__()
            self._model = model
            self._offset = offset

        @override
        def forward(
            self,
            image_6ch: torch.Tensor,
            valid_pixel_mask: torch.Tensor,
            pixel_per_mm: torch.Tensor,
        ) -> tuple[torch.Tensor, torch.Tensor]:
            mean, log_variance = self._model(image_6ch, valid_pixel_mask, pixel_per_mm)
            return mean, log_variance + self._offset

    wrapper = _ExportWrapper(
        weights.model, weights.uncertainty_log_variance_offset
    ).eval()
    example_height, example_width = (64, 64)
    example = (
        torch.zeros((1, 6, example_height, example_width), dtype=torch.float32),
        torch.ones((1, 1, example_height, example_width), dtype=torch.bool),
        torch.ones((1, 1), dtype=torch.float32) * 30.0,
    )
    height_dimension = torch.export.Dim("height", min=32, max=1024)
    width_dimension = torch.export.Dim("width", min=32, max=1024)
    try:
        torch.onnx.export(
            wrapper,
            example,
            destination,
            input_names=("image_6ch", "valid_pixel_mask", "pixel_per_mm"),
            output_names=("mean_volume_ul", "log_variance_volume_ul2"),
            dynamic_shapes=(
                {2: height_dimension, 3: width_dimension},
                {2: height_dimension, 3: width_dimension},
                None,
            ),
            opset_version=opset_version,
            dynamo=True,
            external_data=False,
        )
    except Exception:
        destination.unlink(missing_ok=True)
        raise

    _write_onnx_export_metadata(
        destination,
        weights,
        weights_path=weights_path,
        formal_weights=_formal_weights,
    )
    node_types = _check_onnx_model(destination)
    parity = validate_onnx_parity(
        weights_path,
        destination,
        shapes=shapes,
    )
    if not parity.passed:
        destination.unlink(missing_ok=True)
        raise ValueError("exportしたONNX modelがeager parity gateを満たしません")
    return OnnxExportResult(
        weights_path=Path(weights_path).expanduser().resolve(),
        model_path=destination,
        model_artifact_sha256=_sha256_file(destination),
        opset_version=opset_version,
        node_types=node_types,
        dynamic_shapes=shapes,
        lineage=weights.lineage,
        model_config=weights.model_config,
        preprocess_schema=weights.preprocess_schema,
        uncertainty_log_variance_offset=weights.uncertainty_log_variance_offset,
    )


def export_onnx_model_from_formal_weights(
    formal_weights: FormalTrainingWeights,
    output_path: Path,
    *,
    opset_version: int = DEFAULT_OPSET_VERSION,
    verification_shapes: Sequence[tuple[int, int]] = (
        (32, 32),
        (64, 128),
        (128, 64),
        (512, 512),
        (256, 1024),
    ),
) -> OnnxExportResult:
    """Export only from live-verified FINISHED base-train/finetune weights."""

    return export_onnx_model(
        formal_weights.weights_path,
        output_path,
        opset_version=opset_version,
        verification_shapes=verification_shapes,
        _formal_weights=formal_weights,
    )


def validate_onnx_parity(
    weights_path: Path,
    onnx_path: Path,
    *,
    shapes: Sequence[tuple[int, int]],
    seed: int = 1729,
) -> OnnxParityResult:
    """実ONNX Runtimeでdynamic shapeとeager出力のparityを検証する."""

    import onnxruntime as ort
    import torch

    checked_shapes = _validate_shapes(shapes)
    weights = _load_weights(weights_path)
    model_path = Path(onnx_path).expanduser().resolve(strict=True)
    _check_onnx_model(model_path)
    session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    generator = torch.Generator(device="cpu")
    generator.manual_seed(seed)
    maximum_mean_error = 0.0
    maximum_log_variance_error = 0.0
    passed = True
    weights.model.eval()
    with torch.no_grad():
        for height, width in checked_shapes:
            image = torch.randn(
                (1, 6, height, width), generator=generator, dtype=torch.float32
            )
            mask = torch.ones((1, 1, height, width), dtype=torch.bool)
            scale = torch.tensor([[30.0]], dtype=torch.float32)
            eager_mean, eager_log_variance = weights.model(image, mask, scale)
            eager_log_variance = (
                eager_log_variance + weights.uncertainty_log_variance_offset
            )
            actual_mean, actual_log_variance = session.run(
                ["mean_volume_ul", "log_variance_volume_ul2"],
                {
                    "image_6ch": image.numpy(),
                    "valid_pixel_mask": mask.numpy(),
                    "pixel_per_mm": scale.numpy(),
                },
            )
            eager_mean_value = float(eager_mean.item())
            eager_log_variance_value = float(eager_log_variance.item())
            actual_mean_value = float(np.asarray(actual_mean).item())
            actual_log_variance_value = float(np.asarray(actual_log_variance).item())
            values = (
                eager_mean_value,
                eager_log_variance_value,
                actual_mean_value,
                actual_log_variance_value,
            )
            if (
                not all(math.isfinite(value) for value in values)
                or actual_mean_value <= 0
            ):
                passed = False
                continue
            mean_error = abs(actual_mean_value - eager_mean_value)
            log_variance_error = abs(
                actual_log_variance_value - eager_log_variance_value
            )
            maximum_mean_error = max(maximum_mean_error, mean_error)
            maximum_log_variance_error = max(
                maximum_log_variance_error, log_variance_error
            )
            mean_tolerance = max(
                FP32_ABSOLUTE_TOLERANCE_UL,
                abs(eager_mean_value) * FP32_RELATIVE_TOLERANCE,
            )
            log_variance_tolerance = max(
                FP32_ABSOLUTE_TOLERANCE_UL,
                abs(eager_log_variance_value) * FP32_RELATIVE_TOLERANCE,
            )
            passed = passed and mean_error <= mean_tolerance
            passed = passed and log_variance_error <= log_variance_tolerance
    return OnnxParityResult(
        model_artifact_sha256=_sha256_file(model_path),
        shapes=checked_shapes,
        maximum_mean_absolute_error_ul=maximum_mean_error,
        maximum_log_variance_absolute_error=maximum_log_variance_error,
        passed=passed,
    )


def _load_weights(weights_path: Path) -> _LoadedWeights:
    import torch

    from .model import PasteVolumeModelConfig, PasteVolumeResNet
    from .training import load_model_weights

    path = Path(weights_path).expanduser().resolve(strict=True)
    try:
        raw = load_model_weights(path)
    except Exception as exc:
        raise ValueError(f"weights artifactを読めません: {exc}") from exc
    if raw["source_checkpoint_role"] != "best":
        raise ValueError("export source checkpoint roleはbestだけを許可します")
    if not isinstance(raw["model_config"], dict):
        raise ValueError("model_configはmappingが必要です")
    if not isinstance(raw["state_dict"], dict):
        raise ValueError("state_dictはmappingが必要です")
    if not isinstance(raw["preprocess_schema"], dict):
        raise ValueError("preprocess_schemaはmappingが必要です")
    _validate_canonical_preprocess_schema(raw["preprocess_schema"])
    offset = raw["uncertainty_log_variance_offset"]
    if not _is_finite_number(offset):
        raise ValueError("uncertainty_log_variance_offsetは有限値が必要です")
    parent_run = raw.get("parent_run_id")
    parent_checkpoint = raw.get("parent_checkpoint_id")
    source_run_id = _required_string(raw, "source_run_id")
    source_checkpoint_sha256 = _required_string(raw, "source_checkpoint_sha256")
    dataset_fingerprint = _required_string(raw, "dataset_fingerprint")
    split_fingerprint = _required_string(raw, "split_fingerprint")
    training_protocol_fingerprint = _required_string(
        raw, "training_protocol_fingerprint"
    )
    source_checkpoint_role = _required_best_role(raw)
    run_kind = raw.get("run_kind")
    if run_kind not in ("base-train", "finetune"):
        raise ValueError("weights run_kindが不正です")
    if parent_run is not None and (not isinstance(parent_run, str) or not parent_run):
        raise ValueError("parent_run_idが不正です")
    if parent_checkpoint is not None and (
        not isinstance(parent_checkpoint, str) or not parent_checkpoint
    ):
        raise ValueError("parent_checkpoint_idが不正です")
    try:
        config = PasteVolumeModelConfig(**raw["model_config"])
        model = PasteVolumeResNet(config)
        model.load_state_dict(raw["state_dict"], strict=True)
    except (TypeError, ValueError, RuntimeError) as exc:
        raise ValueError(f"weights model契約が不正です: {exc}") from exc
    for name, tensor in model.state_dict().items():
        if tensor.is_floating_point() and not torch.isfinite(tensor).all():
            raise ValueError(f"state_dictに非有限値があります: {name}")
    model.eval()
    return _LoadedWeights(
        model=model,
        lineage=ArtifactLineage(
            source_run_id=source_run_id,
            source_checkpoint_sha256=source_checkpoint_sha256,
            source_checkpoint_role=source_checkpoint_role,
            dataset_fingerprint=dataset_fingerprint,
            split_fingerprint=split_fingerprint,
            training_protocol_fingerprint=training_protocol_fingerprint,
            parent_run_id=parent_run,
            parent_checkpoint_id=parent_checkpoint,
        ),
        run_kind=cast(Literal["base-train", "finetune"], run_kind),
        model_config=cast(Mapping[str, Any], raw["model_config"]),
        preprocess_schema=cast(Mapping[str, Any], raw["preprocess_schema"]),
        uncertainty_log_variance_offset=float(offset),
    )


def _validate_formal_training_weights(
    formal_weights: FormalTrainingWeights,
    weights_path: Path,
) -> _LoadedWeights:
    if not isinstance(formal_weights, FormalTrainingWeights):
        raise TypeError("formal training weightsはverified typed evidenceが必要です")
    path = Path(weights_path).expanduser().resolve(strict=True)
    if formal_weights.weights_path != path:
        raise ValueError("formal training weights pathが入力artifactと不一致です")
    actual_hash = "sha256:" + _sha256_file(path)
    if formal_weights.weights_sha256 != actual_hash:
        raise ValueError("formal training weights hashが入力artifactと不一致です")
    loaded = _load_weights(path)
    checks = (
        (formal_weights.run_kind, loaded.run_kind),
        (formal_weights.source_run_id, loaded.lineage.source_run_id),
        (formal_weights.dataset_fingerprint, loaded.lineage.dataset_fingerprint),
        (formal_weights.split_fingerprint, loaded.lineage.split_fingerprint),
        (
            formal_weights.training_protocol_fingerprint,
            loaded.lineage.training_protocol_fingerprint,
        ),
    )
    if any(actual != expected for actual, expected in checks):
        raise ValueError("formal training weights lineageがstrict artifactと不一致です")
    attestation = formal_weights.attestation
    if (
        attestation.output_path != path
        or attestation.output_kind != "file"
        or attestation.output_fingerprint != actual_hash
        or attestation.run_id != loaded.lineage.source_run_id
        or attestation.run_kind != loaded.run_kind
        or attestation.status != "FINISHED"
    ):
        raise ValueError("formal training weights attestationがartifactと不一致です")
    return loaded


def _check_onnx_model(path: Path) -> tuple[str, ...]:
    import onnx

    def iter_tensors(message: Any) -> Iterable[Any]:
        descriptor = message.DESCRIPTOR
        if descriptor.full_name == "onnx.TensorProto":
            yield message
            return
        for field, value in message.ListFields():
            if field.message_type is None:
                continue
            if field.is_repeated:
                for item in value:
                    yield from iter_tensors(item)
            else:
                yield from iter_tensors(value)

    model = onnx.load(str(path), load_external_data=False)
    if any(
        tensor.data_location == onnx.TensorProto.EXTERNAL or tensor.external_data
        for tensor in iter_tensors(model)
    ):
        raise ValueError("ONNX external data artifactは許可されません")
    onnx.checker.check_model(model, full_check=True)
    forbidden_domains = sorted(
        {node.domain for node in model.graph.node if node.domain not in {"", "ai.onnx"}}
    )
    if forbidden_domains:
        raise ValueError(
            f"custom ONNX operator domainは許可されません: {forbidden_domains}"
        )
    node_types = {node.op_type for node in model.graph.node}
    unsupported = sorted(node_types - _ALLOWED_ONNX_OPERATORS)
    if unsupported:
        raise ValueError(f"許可されていないONNX operatorがあります: {unsupported}")
    return tuple(sorted(node_types))


def _validate_model_format_graph(path: Path, model_format: ModelFormat) -> None:
    node_types = set(_check_onnx_model(path))
    has_quantize = "QuantizeLinear" in node_types
    has_dequantize = "DequantizeLinear" in node_types
    if model_format == "onnx-fp32" and (has_quantize or has_dequantize):
        raise ValueError("onnx-fp32 model_formatとQ/DQ graphが一致しません")
    if model_format == "onnx-int8-qdq" and not (has_quantize and has_dequantize):
        raise ValueError("onnx-int8-qdq model_formatにはQ/DQ graphが必要です")


def _validate_candidate_artifact(
    path: Path, model_format: ModelFormat
) -> Mapping[str, Any]:
    _validate_model_format_graph(path, model_format)
    metadata = _read_onnx_export_metadata(path)
    expected_role: OnnxArtifactRole = (
        "optimized-fp32" if model_format == "onnx-fp32" else "int8-qdq"
    )
    if metadata.get("artifact_role") != expected_role:
        raise ValueError(
            f"{model_format} candidateにはartifact role {expected_role}が必要です"
        )
    source_export_hash = metadata.get("source_export_sha256")
    if not isinstance(source_export_hash, str) or not _is_sha256(source_export_hash):
        raise ValueError("candidateに元のFP32 export hashがありません")
    if source_export_hash == _sha256_file(path):
        raise ValueError("candidate自身を元のFP32 exportにできません")
    return metadata


def _validate_fp32_export_artifact(path: Path) -> Mapping[str, Any]:
    _validate_model_format_graph(path, "onnx-fp32")
    metadata = _read_onnx_export_metadata(path)
    if metadata.get("artifact_role") != "export-fp32":
        raise ValueError("元のFP32 export artifactが必要です")
    return metadata


def _validate_source_export_hash(
    metadata: Mapping[str, Any],
    *,
    candidate_artifact_sha256: str,
    fp32_reference_artifact_sha256: str,
) -> None:
    if not _is_sha256(fp32_reference_artifact_sha256):
        raise ValueError("FP32 reference artifact hashが不正です")
    if candidate_artifact_sha256 == fp32_reference_artifact_sha256:
        raise ValueError("candidate自身をFP32 referenceにできません")
    if metadata.get("source_export_sha256") != fp32_reference_artifact_sha256:
        raise ValueError("candidateが元のFP32 exportをreferenceにしていません")


def _validate_candidate_reference_artifacts(
    candidate: Path, reference: Path, model_format: ModelFormat
) -> None:
    candidate_hash = _sha256_file(candidate)
    reference_hash = _sha256_file(reference)
    if candidate == reference:
        raise ValueError("candidate自身をFP32 referenceにできません")
    if candidate_hash == reference_hash:
        raise ValueError("candidateとFP32 referenceに同一artifactを指定できません")
    candidate_metadata = _validate_candidate_artifact(candidate, model_format)
    _validate_fp32_export_artifact(reference)
    _validate_source_export_hash(
        candidate_metadata,
        candidate_artifact_sha256=candidate_hash,
        fp32_reference_artifact_sha256=reference_hash,
    )


def optimize_model(
    onnx_model: Path,
    output_directory: Path,
    *,
    calibration_data: Sequence[Path],
    split_manifest: Path,
    calibration_sample_limit: int = 256,
    seed: int = 42,
) -> OptimizationResult:
    """Optimized FP32とtrain split限定のstatic INT8 QDQ候補を生成する.

    splitは再生成せず永続化済みmanifestを必須とし、composite fingerprintと
    全sample集合を照合した後、train assignmentだけからsession均等に選ぶ。
    """

    if type(calibration_sample_limit) is not int or calibration_sample_limit < 1:
        raise ValueError("calibration_sample_limitは1以上の整数が必要です")
    source = Path(onnx_model).expanduser().resolve(strict=True)
    metadata = _validate_fp32_export_artifact(source)
    lineage = _lineage_from_mapping(_required_mapping(metadata, "lineage"))
    preprocess_schema = _required_mapping(metadata, "preprocess_schema")
    constraints = _image_constraints_from_schema(preprocess_schema)
    split_path = Path(split_manifest).expanduser().resolve(strict=True)
    roots = tuple(Path(path).expanduser().resolve() for path in calibration_data)
    if not roots:
        raise ValueError("calibration_dataを1件以上指定してください")

    from .data import (
        build_sample_index,
        load_preprocessed_sample,
        load_split_manifest,
        resolve_dataset_inputs,
        sample_index_fingerprint,
        select_session_balanced_samples,
        validate_split_manifest,
    )

    if len(roots) == 1 and roots[0].is_file():
        composite = resolve_dataset_inputs(manifest=roots[0])
    else:
        composite = resolve_dataset_inputs(roots=roots)
    samples = build_sample_index(composite)
    split = load_split_manifest(
        split_path,
        expected_composite_fingerprint=composite.composite_fingerprint,
    )
    validate_split_manifest(
        split,
        samples,
        expected_composite_fingerprint=composite.composite_fingerprint,
    )
    if composite.composite_fingerprint != lineage.dataset_fingerprint:
        raise ValueError(
            "calibration datasetがtraining dataset fingerprintと不一致です"
        )
    if split.split_fingerprint != lineage.split_fingerprint:
        raise ValueError("persisted splitがtraining split fingerprintと不一致です")
    selected = select_session_balanced_samples(
        samples,
        split.train_sample_ids,
        limit=calibration_sample_limit,
        seed=seed,
    )
    if not selected:
        raise ValueError("train splitにquantization calibration sampleがありません")

    feeds: list[dict[str, np.ndarray[Any, Any]]] = []
    for sample in selected:
        processed = load_preprocessed_sample(
            sample,
            constraints=constraints,
            training=False,
        )
        feeds.append(
            {
                "image_6ch": processed.image_6ch.detach()
                .cpu()
                .float()
                .numpy()[None, ...],
                "valid_pixel_mask": processed.valid_pixel_mask.detach()
                .cpu()
                .numpy()[None, ...]
                .astype(np.bool_),
                "pixel_per_mm": np.asarray(
                    [[processed.pixel_per_mm]], dtype=np.float32
                ),
            }
        )

    destination = Path(output_directory).expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"optimization outputが既に存在します: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    try:
        optimized_path = temporary / "model.optimized.fp32.onnx"
        int8_path = temporary / "model.int8.qdq.onnx"
        _optimize_fp32(source, optimized_path)
        _copy_onnx_export_metadata(
            source, optimized_path, artifact_role="optimized-fp32"
        )
        _quantize_static_qdq(optimized_path, int8_path, feeds)
        _copy_onnx_export_metadata(source, int8_path, artifact_role="int8-qdq")
        _check_onnx_model(optimized_path)
        _check_onnx_model(int8_path)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    final_fp32 = destination / "model.optimized.fp32.onnx"
    final_int8 = destination / "model.int8.qdq.onnx"
    return OptimizationResult(
        source_model_path=source,
        source_model_artifact_sha256=_sha256_file(source),
        optimized_fp32_path=final_fp32,
        optimized_fp32_artifact_sha256=_sha256_file(final_fp32),
        int8_path=final_int8,
        int8_artifact_sha256=_sha256_file(final_int8),
        calibration_sample_ids=tuple(sample.sample_id for sample in selected),
        calibration_session_ids=tuple(sample.session_id for sample in selected),
        calibration_sample_index_fingerprint=sample_index_fingerprint(samples),
        split_manifest_path=split_path,
        lineage=lineage,
    )


def _optimize_fp32(source: Path, output: Path) -> None:
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
    options.optimized_model_filepath = str(output)
    ort.InferenceSession(
        str(source),
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )
    if not output.is_file():
        raise RuntimeError("ONNX Runtime optimized FP32 artifactが生成されませんでした")


def _quantize_static_qdq(
    source: Path,
    output: Path,
    feeds: Sequence[Mapping[str, np.ndarray[Any, Any]]],
) -> None:
    from onnxruntime.quantization import (
        CalibrationDataReader,
        CalibrationMethod,
        QuantFormat,
        QuantType,
        quantize_static,
    )

    class _Reader(CalibrationDataReader):
        def __init__(self) -> None:
            self._iterator = iter(dict(feed) for feed in feeds)

        @override
        def get_next(self) -> dict[Any, Any]:
            return cast(dict[Any, Any], next(self._iterator, None))

        def rewind(self) -> None:
            self._iterator = iter(feeds)

    quantize_static(
        str(source),
        str(output),
        _Reader(),
        quant_format=QuantFormat.QDQ,
        activation_type=QuantType.QInt8,
        weight_type=QuantType.QInt8,
        per_channel=True,
        calibrate_method=CalibrationMethod.MinMax,
        op_types_to_quantize=("Conv", "MatMul", "Gemm"),
        extra_options={"ActivationSymmetric": True, "WeightSymmetric": True},
    )


def _write_onnx_export_metadata(
    path: Path,
    weights: _LoadedWeights,
    *,
    weights_path: Path,
    formal_weights: FormalTrainingWeights | None,
) -> None:
    import onnx
    import torch

    model = onnx.load(str(path), load_external_data=False)
    opset_versions = [
        item.version for item in model.opset_import if item.domain in {"", "ai.onnx"}
    ]
    if not opset_versions:
        raise ValueError("ONNX modelのstandard opsetがありません")
    del model.metadata_props[:]
    metadata = {
        "kind": "pcbasm-paste-volume-onnx-export",
        "schema_version": 1,
        "artifact_role": "export-fp32",
        "training_weights_attestation": (
            formal_weights.attestation.to_dict() if formal_weights is not None else None
        ),
        "source_weights_sha256": "sha256:"
        + _sha256_file(Path(weights_path).expanduser().resolve(strict=True)),
        "lineage": weights.lineage.to_dict(),
        "model_config": dict(weights.model_config),
        "preprocess_schema": dict(weights.preprocess_schema),
        "uncertainty_log_variance_offset": weights.uncertainty_log_variance_offset,
        "export_provenance": {
            "api": "torch.onnx.export",
            "mode": "dynamo",
            "torch_version": torch.__version__,
            "onnx_version": onnx.__version__,
            "opset_version": max(opset_versions),
        },
        "artifact_provenance": {
            "role": "export-fp32",
            "tool": "torch.onnx.export",
            "version": torch.__version__,
        },
    }
    entry = model.metadata_props.add()
    entry.key = "pcbasm.paste_volume.export"
    entry.value = json.dumps(
        metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    onnx.save(model, str(path), save_as_external_data=False)


def _read_onnx_export_metadata(path: Path) -> Mapping[str, Any]:
    import onnx

    model = onnx.load(str(path), load_external_data=False)
    values = {
        entry.key: entry.value
        for entry in model.metadata_props
        if entry.key == "pcbasm.paste_volume.export"
    }
    raw = values.get("pcbasm.paste_volume.export")
    if raw is None:
        raise ValueError("ONNX modelにtraining lineage metadataがありません")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("ONNX training lineage metadataが不正です") from exc
    if not isinstance(value, dict):
        raise ValueError("ONNX training lineage metadataはobjectが必要です")
    if value.get("kind") != "pcbasm-paste-volume-onnx-export":
        raise ValueError("ONNX training lineage metadata kindが不正です")
    if value.get("schema_version") != 1:
        raise ValueError("未知のONNX training lineage metadata schemaです")
    artifact_role = value.get("artifact_role")
    base_keys = {
        "kind",
        "schema_version",
        "artifact_role",
        "training_weights_attestation",
        "source_weights_sha256",
        "lineage",
        "model_config",
        "preprocess_schema",
        "uncertainty_log_variance_offset",
        "export_provenance",
        "artifact_provenance",
    }
    expected_keys = (
        base_keys
        if artifact_role == "export-fp32"
        else base_keys | {"source_export_sha256"}
    )
    if artifact_role not in {"export-fp32", "optimized-fp32", "int8-qdq"}:
        raise ValueError("ONNX artifact roleが不正です")
    if set(value) != expected_keys:
        raise ValueError("ONNX training lineage metadata key集合が不正です")
    source_weights_sha256 = value.get("source_weights_sha256")
    if not isinstance(source_weights_sha256, str) or not _is_prefixed_sha256(
        source_weights_sha256
    ):
        raise ValueError("ONNX source weights fingerprintが不正です")
    lineage = _lineage_from_mapping(_required_mapping(value, "lineage"))
    training_attestation = value.get("training_weights_attestation")
    if training_attestation is not None:
        if not isinstance(training_attestation, Mapping):
            raise ValueError("ONNX training weights attestationが不正です")
        try:
            attestation = FormalArtifactAttestation.from_dict(training_attestation)
        except ValueError as exc:
            raise ValueError("ONNX training weights attestationが不正です") from exc
        if (
            attestation.output_kind != "file"
            or attestation.output_fingerprint != source_weights_sha256
            or attestation.run_id != lineage.source_run_id
            or attestation.run_kind not in ("base-train", "finetune")
        ):
            raise ValueError("ONNX training weights attestationがlineageと不一致です")
    if artifact_role != "export-fp32":
        source_export = value.get("source_export_sha256")
        if not isinstance(source_export, str) or not _is_sha256(source_export):
            raise ValueError("ONNX source export fingerprintが不正です")
    offset = value.get("uncertainty_log_variance_offset")
    if not _is_finite_number(offset):
        raise ValueError("ONNX uncertainty calibration offsetが不正です")
    export_provenance = _required_mapping(value, "export_provenance")
    if set(export_provenance) != {
        "api",
        "mode",
        "torch_version",
        "onnx_version",
        "opset_version",
    }:
        raise ValueError("ONNX export provenance key集合が不正です")
    if (
        export_provenance.get("api") != "torch.onnx.export"
        or export_provenance.get("mode") != "dynamo"
        or not isinstance(export_provenance.get("torch_version"), str)
        or not export_provenance["torch_version"]
        or not isinstance(export_provenance.get("onnx_version"), str)
        or not export_provenance["onnx_version"]
        or type(export_provenance.get("opset_version")) is not int
        or int(export_provenance["opset_version"]) < 18
    ):
        raise ValueError("ONNX export provenanceが不正です")
    opset_versions = [
        item.version for item in model.opset_import if item.domain in {"", "ai.onnx"}
    ]
    if not opset_versions or export_provenance["opset_version"] != max(opset_versions):
        raise ValueError("ONNX export provenanceのopsetがgraphと不一致です")
    artifact_provenance = _required_mapping(value, "artifact_provenance")
    if set(artifact_provenance) != {"role", "tool", "version"}:
        raise ValueError("ONNX artifact provenance key集合が不正です")
    expected_tool = (
        "torch.onnx.export" if artifact_role == "export-fp32" else "onnxruntime"
    )
    if (
        artifact_provenance.get("role") != artifact_role
        or artifact_provenance.get("tool") != expected_tool
        or not isinstance(artifact_provenance.get("version"), str)
        or not artifact_provenance["version"]
    ):
        raise ValueError("ONNX artifact provenanceが不正です")
    return cast(Mapping[str, Any], value)


def _copy_onnx_export_metadata(
    source: Path,
    destination: Path,
    *,
    artifact_role: Literal["optimized-fp32", "int8-qdq"],
) -> None:
    import onnx
    import onnxruntime as ort

    source_metadata = _read_onnx_export_metadata(source)
    if source_metadata.get("artifact_role") != "export-fp32":
        raise ValueError("optimization sourceは元のFP32 exportが必要です")
    metadata = {
        **source_metadata,
        "artifact_role": artifact_role,
        "source_export_sha256": _sha256_file(source),
        "artifact_provenance": {
            "role": artifact_role,
            "tool": "onnxruntime",
            "version": ort.__version__,
        },
    }
    model = onnx.load(str(destination), load_external_data=False)
    retained = [
        (entry.key, entry.value)
        for entry in model.metadata_props
        if entry.key != "pcbasm.paste_volume.export"
    ]
    del model.metadata_props[:]
    for key, value in retained:
        entry = model.metadata_props.add()
        entry.key = key
        entry.value = value
    entry = model.metadata_props.add()
    entry.key = "pcbasm.paste_volume.export"
    entry.value = json.dumps(
        metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    onnx.save(model, str(destination), save_as_external_data=False)


def _validate_shapes(
    shapes: Sequence[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    checked: list[tuple[int, int]] = []
    for shape in shapes:
        if len(shape) != 2 or any(
            type(value) is not int or value < 32 for value in shape
        ):
            raise ValueError("verification shapeは32以上の(H, W)が必要です")
        height, width = shape
        if max(height, width) > 1024 or height * width > 262_144:
            raise ValueError("verification shapeが画像制約を超えています")
        checked.append((height, width))
    if not checked:
        raise ValueError("verification shapeが必要です")
    return tuple(checked)


def _validate_benchmark_iterations(benchmark: ModelBenchmarkResult) -> None:
    if (
        type(benchmark.warmup_iterations) is not int
        or benchmark.warmup_iterations != BENCHMARK_WARMUP_ITERATIONS
    ):
        raise ValueError(
            f"production benchmark warm-upは{BENCHMARK_WARMUP_ITERATIONS}回が必要です"
        )
    if (
        type(benchmark.measured_iterations) is not int
        or benchmark.measured_iterations != BENCHMARK_MEASURED_ITERATIONS
    ):
        raise ValueError(
            f"production benchmark測定は{BENCHMARK_MEASURED_ITERATIONS}回が必要です"
        )
    if benchmark.schema_version != BENCHMARK_SCHEMA_VERSION:
        raise ValueError("未知のbenchmark report schemaです")
    if tuple(result.category for result in benchmark.category_results) != (
        _BENCHMARK_CATEGORIES
    ):
        raise ValueError(
            "benchmark categoryはsmall/medium/large/portrait/landscapeの固定順が必要です"
        )
    if benchmark.sample_count != len(_BENCHMARK_CATEGORIES):
        raise ValueError("benchmark sample_countは5が必要です")
    if benchmark.benchmark_sample_ids != tuple(
        result.sample_id for result in benchmark.category_results
    ):
        raise ValueError("benchmark sample IDとcategory証跡が不一致です")
    if len(set(benchmark.benchmark_sample_ids)) != len(_BENCHMARK_CATEGORIES):
        raise ValueError("benchmark category間でsampleを再利用できません")
    for result in benchmark.category_results:
        if not result.gate_passed:
            raise ValueError(
                f"benchmark category証跡が不正またはp95 gate失敗です: {result.category}"
            )
    numeric_values = (
        benchmark.cold_latency_ms,
        benchmark.p50_latency_ms,
        benchmark.p95_latency_ms,
        benchmark.p99_latency_ms,
    )
    if not all(_is_nonnegative_finite(value) for value in numeric_values):
        raise ValueError("benchmark latencyは非負の有限値が必要です")
    if benchmark.p95_latency_ms != max(
        result.p95_latency_ms for result in benchmark.category_results
    ):
        raise ValueError("benchmark aggregate p95がcategory最悪値と不一致です")
    if benchmark.p99_latency_ms != max(
        result.p99_latency_ms for result in benchmark.category_results
    ):
        raise ValueError("benchmark aggregate p99がcategory最悪値と不一致です")
    if type(benchmark.peak_rss_bytes) is not int or benchmark.peak_rss_bytes < 0:
        raise ValueError("benchmark peak_rss_bytesが不正です")
    if (
        type(benchmark.artifact_size_bytes) is not int
        or benchmark.artifact_size_bytes < 1
    ):
        raise ValueError("benchmark artifact_size_bytesが不正です")
    if benchmark.cold_start_clock != "CLOCK_MONOTONIC":
        raise ValueError("benchmark cold start clockはCLOCK_MONOTONICが必要です")
    for name in (
        "cold_start_origin",
        "os_id",
        "os_release",
        "python_version",
        "onnxruntime_version",
        "cpu_governor",
        "platform_machine",
        "power_condition",
        "cooling_condition",
        "platform_model",
    ):
        value = getattr(benchmark, name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"benchmark {name}を空にできません")
    for name in (
        "source_checkpoint_sha256",
        "training_dataset_fingerprint",
        "training_split_fingerprint",
        "training_protocol_fingerprint",
        "dataset_fingerprint",
        "split_fingerprint",
    ):
        if not _is_prefixed_sha256(getattr(benchmark, name)):
            raise ValueError(f"benchmark {name}が不正です")
    if benchmark.source_checkpoint_role != "best":
        raise ValueError("benchmark source checkpoint roleはbestが必要です")
    if (benchmark.parent_run_id is None) != (benchmark.parent_checkpoint_id is None):
        raise ValueError("benchmark fine-tune parent lineageが片方だけです")


def _validate_evaluation_gate_numbers(evaluation: CandidateEvaluation) -> None:
    """保存boolを信用せずpromotion gateを数値から再計算する."""

    failures: list[str] = []
    numeric_values = (
        evaluation.primary_accuracy_score,
        evaluation.coverage_68,
        evaluation.fp32_reference_accuracy_score,
        evaluation.fp32_reference_coverage_68,
        evaluation.maximum_mean_parity_error_ul,
        evaluation.maximum_mean_parity_tolerance_ul,
        evaluation.maximum_mean_parity_ratio,
        evaluation.maximum_log_variance_parity_error,
        evaluation.maximum_log_variance_parity_tolerance,
        evaluation.maximum_log_variance_parity_ratio,
        evaluation.padding_accuracy_degradation,
        evaluation.uncertainty_relative_std_threshold,
        evaluation.accepted_accuracy_score,
        evaluation.accepted_sample_coverage,
        evaluation.three_sample_acceptance,
    )
    if not all(_is_finite_number(value) for value in numeric_values):
        raise ValueError("evaluation reportに非有限値があります")
    if evaluation.sample_count < 1:
        failures.append("sample_count")
    if evaluation.primary_accuracy_score > PRIMARY_ACCURACY_MAX:
        failures.append("primary_accuracy")
    if evaluation.padding_accuracy_degradation > PADDING_DEGRADATION_MAX:
        failures.append("padding_invariance")
    if evaluation.model_format == "onnx-fp32":
        if (
            evaluation.maximum_mean_parity_ratio > 1.0
            or evaluation.maximum_log_variance_parity_ratio > 1.0
            or not evaluation.fp32_parity_passed
        ):
            failures.append("fp32_export_parity")
    else:
        if (
            evaluation.primary_accuracy_score - evaluation.fp32_reference_accuracy_score
            > INT8_ACCURACY_DEGRADATION_MAX
        ):
            failures.append("int8_accuracy_degradation")
        if (
            abs(evaluation.coverage_68 - evaluation.fp32_reference_coverage_68)
            > INT8_COVERAGE_DELTA_MAX
        ):
            failures.append("int8_coverage_delta")
    if evaluation.accepted_accuracy_score > PRIMARY_ACCURACY_MAX:
        failures.append("uncertainty_accepted_accuracy")
    if not 0.0 < evaluation.accepted_sample_coverage <= 1.0:
        failures.append("accepted_sample_coverage")
    if evaluation.phase == "frozen_test" and (
        evaluation.three_sample_acceptance < THREE_SAMPLE_ACCEPTANCE_MIN
    ):
        failures.append("three_sample_acceptance")
    if failures or evaluation.gate_failures or not evaluation.gate_passed:
        raise ValueError(
            "evaluation gate数値を再検証できません: "
            f"stored={evaluation.gate_failures}, recalculated={tuple(failures)}"
        )


def _validate_canonical_preprocess_schema(schema: Mapping[str, Any]) -> None:
    expected_keys = {
        "schema_version",
        "channel_order",
        "input_channels",
        "normalization",
        "image_constraints",
    }
    if set(schema) != expected_keys:
        raise ValueError("preprocess schemaのkey集合が正規v1と一致しません")
    if schema.get("schema_version") != 1:
        raise ValueError("未知のpreprocess schemaです")
    if schema.get("channel_order") != "RGB" or schema.get("input_channels") != 6:
        raise ValueError("preprocessはRGB 6-channelが必要です")
    normalization = _required_mapping(schema, "normalization")
    if set(normalization) != {
        "kind",
        "axes",
        "affine",
        "per_channel",
        "epsilon",
    }:
        raise ValueError("normalization schemaのkey集合が不正です")
    if (
        normalization.get("kind") != "sample-layer-norm"
        or normalization.get("axes") != [0, 1, 2]
        or normalization.get("affine") is not False
        or normalization.get("per_channel") is not False
        or not _is_positive_finite(normalization.get("epsilon"))
    ):
        raise ValueError("全6-channel sample単位LayerNorm schemaが必要です")
    constraints = _required_mapping(schema, "image_constraints")
    if set(constraints) != {
        "min_size",
        "max_size",
        "max_pixels",
        "stride",
        "normalization_epsilon",
    }:
        raise ValueError("image_constraints schemaのkey集合が不正です")
    min_size = constraints.get("min_size")
    max_size = constraints.get("max_size")
    max_pixels = constraints.get("max_pixels")
    stride = constraints.get("stride")
    constraint_epsilon = constraints.get("normalization_epsilon")
    if (
        type(min_size) is not int
        or type(max_size) is not int
        or type(max_pixels) is not int
        or type(stride) is not int
        or min_size < 32
        or max_size < min_size
        or max_size > 1024
        or max_pixels < min_size * min_size
        or max_pixels > 262144
        or stride < 1
        or stride > max_size
        or not _is_positive_finite(constraint_epsilon)
    ):
        raise ValueError("画像制約がv1安全範囲を満たしません")
    if not math.isclose(
        float(normalization["epsilon"]),
        float(constraint_epsilon),
        rel_tol=0.0,
        abs_tol=0.0,
    ):
        raise ValueError("normalization epsilonがimage constraintsと不一致です")


def _image_constraints_from_schema(schema: Mapping[str, Any]) -> Any:
    from .data import ImageConstraints

    _validate_canonical_preprocess_schema(schema)
    raw = _required_mapping(schema, "image_constraints")
    return ImageConstraints(
        min_size=int(raw["min_size"]),
        max_size=int(raw["max_size"]),
        max_pixels=int(raw["max_pixels"]),
        stride=int(raw["stride"]),
        normalization_epsilon=float(raw["normalization_epsilon"]),
    )


def _gate_spec() -> dict[str, float]:
    return {
        "primary_accuracy_max": PRIMARY_ACCURACY_MAX,
        "fp32_relative_tolerance": FP32_RELATIVE_TOLERANCE,
        "fp32_absolute_tolerance_ul": FP32_ABSOLUTE_TOLERANCE_UL,
        "padding_degradation_max": PADDING_DEGRADATION_MAX,
        "int8_accuracy_degradation_max": INT8_ACCURACY_DEGRADATION_MAX,
        "int8_coverage_delta_max": INT8_COVERAGE_DELTA_MAX,
        "pi_p95_latency_ms_max": PI_P95_LATENCY_MS_MAX,
        "three_sample_acceptance_min": THREE_SAMPLE_ACCEPTANCE_MIN,
    }


def _evaluation_lineage(evaluation: CandidateEvaluation) -> ArtifactLineage:
    return ArtifactLineage(
        source_run_id=evaluation.source_run_id,
        source_checkpoint_sha256=evaluation.source_checkpoint_sha256,
        source_checkpoint_role=evaluation.source_checkpoint_role,
        dataset_fingerprint=evaluation.training_dataset_fingerprint,
        split_fingerprint=evaluation.training_split_fingerprint,
        training_protocol_fingerprint=evaluation.training_protocol_fingerprint,
        parent_run_id=evaluation.parent_run_id,
        parent_checkpoint_id=evaluation.parent_checkpoint_id,
    )


def _lineage_from_mapping(value: Mapping[str, Any]) -> ArtifactLineage:
    if set(value) != {
        "source_run_id",
        "source_checkpoint_sha256",
        "source_checkpoint_role",
        "dataset_fingerprint",
        "split_fingerprint",
        "training_protocol_fingerprint",
        "parent_run_id",
        "parent_checkpoint_id",
    }:
        raise ValueError("training lineage key集合が不正です")
    return ArtifactLineage(
        source_run_id=_required_string(value, "source_run_id"),
        source_checkpoint_sha256=_required_string(value, "source_checkpoint_sha256"),
        source_checkpoint_role=_required_best_role(value),
        dataset_fingerprint=_required_string(value, "dataset_fingerprint"),
        split_fingerprint=_required_string(value, "split_fingerprint"),
        training_protocol_fingerprint=_required_string(
            value, "training_protocol_fingerprint"
        ),
        parent_run_id=_optional_string(value, "parent_run_id"),
        parent_checkpoint_id=_optional_string(value, "parent_checkpoint_id"),
    )


def _required_mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    item = value.get(key)
    if not isinstance(item, dict):
        raise ValueError(f"{key}はobjectが必要です")
    return cast(Mapping[str, Any], item)


def _required_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{key}は空でない文字列が必要です")
    return item


def _required_best_role(value: Mapping[str, Any]) -> Literal["best"]:
    if value.get("source_checkpoint_role") != "best":
        raise ValueError("source_checkpoint_roleはbestが必要です")
    return "best"


def _optional_string(value: Mapping[str, Any], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or not item:
        raise ValueError(f"{key}はnullまたは空でない文字列が必要です")
    return item


def _validate_rgb_array(value: object, name: str) -> None:
    if not isinstance(value, np.ndarray):
        raise TypeError(f"{name}はnumpy.ndarrayが必要です")
    if value.dtype != np.uint8 or value.ndim != 3 or value.shape[2] != 3:
        raise ValueError(f"{name}はuint8 HWC RGB画像が必要です")


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_json_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _is_prefixed_sha256(value: str) -> bool:
    return value.startswith("sha256:") and _is_sha256(value.removeprefix("sha256:"))


def _is_finite_number(value: object) -> TypeGuard[int | float]:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _is_positive_finite(value: object) -> TypeGuard[int | float]:
    return _is_finite_number(value) and value > 0


def _is_nonnegative_finite(value: object) -> TypeGuard[int | float]:
    return _is_finite_number(value) and value >= 0


def _major_minor_version(value: str) -> str:
    parts = value.split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise ValueError(f"runtime versionを解釈できません: {value}")
    return f"{int(parts[0])}.{int(parts[1])}"


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"JSONを読めません: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON objectが必要です: {path}")
    return cast(Mapping[str, Any], value)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(
                value,
                stream,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json_new(path: Path, value: Mapping[str, Any]) -> None:
    """既存artifactを置換せず、新規JSON reportだけをatomicに公開する."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".new.tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(
                value,
                stream,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
