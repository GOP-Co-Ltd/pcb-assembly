"""Paste-volume release candidate evaluation and selection policy."""

from __future__ import annotations

import math
import statistics
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, cast

import attrs
import numpy as np

from ml.infer.onnx import OnnxSession

from .artifact import (
    EVALUATION_SCHEMA_VERSION,
    ArtifactLineage,
    ModelFormat,
    artifact_sha256 as _sha256_file,
    atomic_json_new as _atomic_json_new,
    canonical_json as _canonical_json,
    image_constraints_from_schema as _image_constraints_from_schema,
    is_finite_number as _is_finite_number,
    is_positive_finite as _is_positive_finite,
    lineage_from_mapping as _lineage_from_mapping,
    read_json as _read_json,
    required_mapping as _required_mapping,
    write_json as _write_json,
)
from .benchmark import (
    PI_P95_LATENCY_MS_MAX,
    ModelBenchmarkResult,
    load_model_benchmark_result,
    validate_benchmark_result,
)
from .compile_parity import (
    CompileParityBatch,
    CompileParityConfig,
    CompileParityReport,
    compile_parity_report_from_dict,
    run_compile_parity,
    validate_compile_parity_report,
)
from .formal_artifact import FormalArtifactAttestation
from .onnx import (
    FP32_ABSOLUTE_TOLERANCE_UL,
    FP32_RELATIVE_TOLERANCE,
    ExportParityReport,
    check_onnx_model,
    export_parity_report_from_dict,
    load_export_weights,
    read_onnx_artifact_lineage,
    read_onnx_preprocess_schema,
    resolve_evaluation_dataset,
    run_processed_onnx,
    validate_candidate_artifact,
    validate_candidate_reference_artifacts,
    validate_export_parity_report,
    validate_formal_training_weights,
    validate_source_export_hash,
)
from .training_artifacts import FormalTrainingWeights

PRIMARY_ACCURACY_MAX = 0.10
PADDING_DEGRADATION_MAX = 0.01
INT8_ACCURACY_DEGRADATION_MAX = 0.01
INT8_COVERAGE_DELTA_MAX = 0.03
THREE_SAMPLE_ACCEPTANCE_MIN = 0.90
_PADDING_CONDITIONS = (
    ("padding-10-top", 0.10, "top"),
    ("padding-25-right", 0.25, "right"),
    ("padding-50-bottom", 0.50, "bottom"),
)
_REQUIRED_PADDING_CONDITION_LABELS = frozenset(
    label for label, _, _ in _PADDING_CONDITIONS
)


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
class _DatasetSplitPredictions:
    predictions: tuple[CandidatePrediction, ...]
    dataset_fingerprint: str
    split_fingerprint: str
    training_sample_ids: tuple[str, ...]


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
    validate_candidate_reference_artifacts(candidate, reference, model_format)
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
    validate_candidate_reference_artifacts(
        candidate, reference, validation.model_format
    )
    lineage = evaluation_lineage(validation)
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
    import torch
    from torch.nn import functional as torch_functional

    from .data import load_preprocessed_sample

    _candidate_nodes = check_onnx_model(candidate_model)
    reference_nodes = check_onnx_model(reference_model)
    if "QuantizeLinear" in reference_nodes or "DequantizeLinear" in reference_nodes:
        raise ValueError("FP32 referenceにquantized operatorがあります")
    candidate_schema = read_onnx_preprocess_schema(candidate_model)
    reference_schema = read_onnx_preprocess_schema(reference_model)
    if _canonical_json(candidate_schema) != _canonical_json(reference_schema):
        raise ValueError("candidateとFP32 referenceのpreprocess schemaが不一致です")
    constraints = _image_constraints_from_schema(candidate_schema)
    composite, samples, split = resolve_evaluation_dataset(
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
    candidate_session = OnnxSession.load_cpu(candidate_model)
    reference_session = OnnxSession.load_cpu(reference_model)
    predictions: list[CandidatePrediction] = []
    for sample_id in ids:
        sample = by_id[sample_id]
        processed = load_preprocessed_sample(
            sample, constraints=constraints, training=False
        )
        candidate_mean, candidate_log_variance = run_processed_onnx(
            candidate_session,
            processed.image_6ch,
            processed.valid_pixel_mask,
            processed.pixel_per_mm,
        )
        reference_mean, reference_log_variance = run_processed_onnx(
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
            padded_mean, _ = run_processed_onnx(
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

    weights = load_export_weights(weights_path)
    _, samples, split = resolve_evaluation_dataset(
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

    validate_formal_training_weights(formal_weights, formal_weights.weights_path)
    return run_compile_parity_from_dataset(
        formal_weights.weights_path,
        dataset_paths,
        split_manifest,
        config=config,
    )


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
    embedded = validate_candidate_artifact(path, model_format)
    if _lineage_from_mapping(_required_mapping(embedded, "lineage")) != lineage:
        raise ValueError("validation modelのembedded lineageが不一致です")
    validate_source_export_hash(
        embedded,
        candidate_artifact_sha256=artifact_hash,
        fp32_reference_artifact_sha256=fp32_reference_artifact_sha256,
    )
    candidate_id = make_candidate_id(model_format, artifact_hash)
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
    metadata = validate_candidate_artifact(path, evaluation.model_format)
    validate_release_compile_parity(compile_parity)
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
    validate_benchmark_result(benchmark)
    validate_source_export_hash(
        metadata,
        candidate_artifact_sha256=actual_hash,
        fp32_reference_artifact_sha256=(evaluation.fp32_reference_artifact_sha256),
    )
    lineage = _lineage_from_mapping(_required_mapping(metadata, "lineage"))
    formal_training_attestation_from_metadata(metadata, lineage)
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


def validate_release_compile_parity(report: CompileParityReport) -> None:
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


def formal_training_attestation_from_metadata(
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
    metadata = validate_candidate_artifact(
        selection.candidate.model_path, validation.model_format
    )
    actual_hash = _sha256_file(selection.candidate.model_path)
    if actual_hash != validation.model_artifact_sha256:
        raise ValueError("選択後にmodel artifactが変更されています")
    validate_source_export_hash(
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


def make_candidate_id(model_format: ModelFormat, artifact_hash: str) -> str:
    return f"{model_format}:{artifact_hash[:16]}"


def validate_evaluation_gate_numbers(evaluation: CandidateEvaluation) -> None:
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


def release_gate_spec() -> dict[str, float]:
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


def evaluation_lineage(evaluation: CandidateEvaluation) -> ArtifactLineage:
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


def _major_minor_version(value: str) -> str:
    parts = value.split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise ValueError(f"runtime versionを解釈できません: {value}")
    return f"{int(parts[0])}.{int(parts[1])}"
