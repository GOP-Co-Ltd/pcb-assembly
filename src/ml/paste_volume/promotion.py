"""Paste-volume packaging, promotion evidence, and activation workflow."""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, cast

import attrs

from ml.export.onnx import standard_opset_version
from ml.export.package import publish_immutable_package, switch_active_pointer
from ml.infer.onnx import onnxruntime_version
from ml.infer.package import (
    ImmutablePackageError,
    load_active_pointer,
)

from .artifact import (
    BENCHMARK_SCHEMA_VERSION,
    EVALUATION_SCHEMA_VERSION,
    MODEL_PACKAGE_SCHEMA_VERSION,
    ArtifactLineage,
    ModelFormat,
    artifact_sha256 as _sha256_file,
    canonical_json as _canonical_json,
    fingerprint_json as _canonical_json_sha256,
    is_finite_number as _is_finite_number,
    is_positive_finite as _is_positive_finite,
    lineage_from_mapping as _lineage_from_mapping,
    required_mapping as _required_mapping,
    required_string as _required_string,
    validate_canonical_preprocess_schema as _validate_canonical_preprocess_schema,
    write_json as _write_json,
)
from .cross_validation import CrossValidationResult
from .formal_artifact import FormalArtifactAttestation
from .onnx import OnnxExportResult, OptimizationResult
from .package import (
    ACTIVE_MODEL_POINTER_SCHEMA_VERSION,
    ActiveModelPointer,
    verify_model_package,
)
from .release import (
    CandidateEvaluation,
    CandidatePrediction,
    FinalizedModelCandidate,
    ModelCandidateValidation,
    SelectedModelCandidate,
)

CROSS_VALIDATION_PROMOTION_EVIDENCE_SCHEMA_VERSION = 1
CROSS_VALIDATION_PROMOTION_EVIDENCE_KIND = (
    "pcbasm-paste-volume-cross-validation-promotion-evidence"
)


def _major_minor_version(value: str) -> str:
    parts = value.split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise ValueError(f"runtime versionを解釈できません: {value}")
    return f"{int(parts[0])}.{int(parts[1])}"


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
class CrossValidationPromotionEvidence:
    """One strict cross-validation report bound to its formal summary run."""

    report: CrossValidationResult
    summary_attestation: FormalArtifactAttestation
    kind: str = CROSS_VALIDATION_PROMOTION_EVIDENCE_KIND
    schema_version: int = CROSS_VALIDATION_PROMOTION_EVIDENCE_SCHEMA_VERSION

    def __attrs_post_init__(self) -> None:
        from .cross_validation import CrossValidationResult
        from .formal_artifact import FormalArtifactAttestation

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

    from .cross_validation import load_cross_validation_result
    from .formal_artifact import verify_formal_artifact

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
        from .onnx import export_onnx_model

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
        from .onnx import optimize_model

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
        from .release import select_model_candidate

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
        from .release import evaluate_frozen_test_candidate_from_dataset

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
        from .release import finalize_selected_model_candidate

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

    from .release import evaluation_lineage, validate_evaluation_gate_numbers

    evaluation = candidate.evaluation
    if evaluation.phase != "validation":
        raise ValueError("candidate packageにはvalidation evaluationが必要です")
    validate_evaluation_gate_numbers(evaluation)
    lineage = evaluation_lineage(evaluation)
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

    from .onnx import read_onnx_preprocess_schema, resolve_evaluation_dataset
    from .release import evaluation_lineage

    evaluation = candidate.evaluation
    lineage = evaluation_lineage(evaluation)
    composite, samples, split = resolve_evaluation_dataset(
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

    from .benchmark import validate_benchmark_result
    from .onnx import read_onnx_export_metadata
    from .release import (
        evaluation_lineage,
        release_gate_spec,
        validate_evaluation_gate_numbers,
    )

    validation = finalized.selection.candidate.evaluation
    benchmark = finalized.selection.candidate.benchmark
    frozen_test = finalized.frozen_test
    if not validation.gate_passed:
        raise ValueError("validation gateを通過していません")
    validate_evaluation_gate_numbers(validation)
    validate_evaluation_gate_numbers(frozen_test)
    if not benchmark.gate_passed:
        raise ValueError("Raspberry Pi 5 p95 benchmark gateを通過していません")
    if benchmark.schema_version != BENCHMARK_SCHEMA_VERSION:
        raise ValueError("未知のbenchmark schemaです")
    validate_benchmark_result(benchmark)
    model_path = finalized.selection.candidate.model_path
    actual_hash = _sha256_file(model_path)
    if actual_hash != validation.model_artifact_sha256:
        raise ValueError("promotion前にmodel artifactが変更されています")
    lineage = evaluation_lineage(validation)
    validated_cross_validation = _validate_cross_validation_release_evidence(
        cross_validation_evidence,
        lineage=lineage,
        dataset_sample_ids=training_dataset_sample_ids,
    )
    metadata = read_onnx_export_metadata(model_path)
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
            "gate_spec": release_gate_spec(),
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

    from .onnx import read_onnx_preprocess_schema, resolve_evaluation_dataset
    from .release import evaluation_lineage

    validation = finalized.selection.candidate.evaluation
    lineage = evaluation_lineage(validation)
    composite, samples, split = resolve_evaluation_dataset(
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
        frozen_composite, _, frozen_split = resolve_evaluation_dataset(
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

    from .onnx import resolve_evaluation_dataset

    _, samples, split = resolve_evaluation_dataset(
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

    from .cross_validation import load_cross_validation_result

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

    package_path = Path(model_package).expanduser().resolve()
    pointer_path = Path(pointer_file).expanduser().resolve()
    if pointer_path.is_relative_to(package_path):
        raise ValueError(
            "active model pointerはimmutable model packageの外に配置してください"
        )
    verified = verify_model_package(package_path, require_promoted=True)
    model = _required_mapping(verified.manifest, "model")
    model_id = _required_string(model, "id")
    pointer = switch_active_pointer(
        pointer_path,
        verified.path,
        active_package_id=model_id,
        active_package_sha256=verified.model_sha256,
        schema_version=ACTIVE_MODEL_POINTER_SCHEMA_VERSION,
    )
    return ActiveModelPointer(
        pointer_path=pointer.pointer_path,
        active_model_path=pointer.active_package_path,
        previous_model_path=pointer.previous_package_path,
        active_model_id=pointer.active_package_id,
        active_model_sha256=pointer.active_package_sha256,
    )


def rollback_active_model(pointer_file: Path) -> ActiveModelPointer:
    """保持中の直前packageを再検証し、active/previousを手動で入れ替える."""

    pointer_path = Path(pointer_file).expanduser().resolve(strict=True)
    current = _load_active_pointer(pointer_path)
    if current.previous_model_path is None:
        raise ValueError("rollback対象のprevious modelがありません")
    verified = verify_model_package(current.previous_model_path, require_promoted=True)
    model = _required_mapping(verified.manifest, "model")
    model_id = _required_string(model, "id")
    pointer = switch_active_pointer(
        pointer_path,
        verified.path,
        active_package_id=model_id,
        active_package_sha256=verified.model_sha256,
        schema_version=ACTIVE_MODEL_POINTER_SCHEMA_VERSION,
    )
    return ActiveModelPointer(
        pointer_path=pointer.pointer_path,
        active_model_path=pointer.active_package_path,
        previous_model_path=pointer.previous_package_path,
        active_model_id=pointer.active_package_id,
        active_model_sha256=pointer.active_package_sha256,
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
    from .compile_parity import compile_parity_report_from_dict
    from .onnx import (
        export_parity_report_from_dict,
        read_onnx_export_metadata,
        validate_candidate_artifact,
        validate_source_export_hash,
    )
    from .release import (
        formal_training_attestation_from_metadata,
        make_candidate_id,
        validate_release_compile_parity,
    )

    _validate_canonical_preprocess_schema(preprocess_schema)
    if not model_name or not model_version:
        raise ValueError("model_name/model_versionを空にできません")
    if not _is_positive_finite(uncertainty_threshold):
        raise ValueError("uncertainty thresholdは正の有限値が必要です")
    source = Path(model_path).expanduser().resolve(strict=True)
    validate_candidate_artifact(source, model_format)
    artifact_hash = _sha256_file(source)
    if candidate_id != make_candidate_id(model_format, artifact_hash):
        raise ValueError("candidate_idがmodel artifact fingerprintと不一致です")
    metadata = read_onnx_export_metadata(source)
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
    validate_release_compile_parity(compile_parity)
    if not export_parity.success:
        raise ValueError("export parity gateを通過していません")
    validate_source_export_hash(
        metadata,
        candidate_artifact_sha256=artifact_hash,
        fp32_reference_artifact_sha256=_required_string(
            validation_payload, "fp32_reference_artifact_sha256"
        ),
    )
    if _lineage_from_mapping(_required_mapping(metadata, "lineage")) != lineage:
        raise ValueError("package modelのembedded lineageが不一致です")
    training_weights_attestation = formal_training_attestation_from_metadata(
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
    runtime_version = onnxruntime_version()
    opset_version = standard_opset_version(source)

    def write_payloads(temporary: Path) -> None:
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
                "minimum_version": _major_minor_version(runtime_version),
                "exporter": "torch.onnx.export(dynamo=True)",
                "opset_version": opset_version,
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

    published = publish_immutable_package(
        output_directory,
        payload_files=(
            "manifest.json",
            "model.onnx",
            "preprocess.json",
            "evaluation.json",
        ),
        write_payloads=write_payloads,
    )
    destination = published.path
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
    try:
        pointer = load_active_pointer(
            path,
            schema_version=ACTIVE_MODEL_POINTER_SCHEMA_VERSION,
        )
    except ImmutablePackageError as exc:
        raise ValueError(str(exc)) from exc
    return ActiveModelPointer(
        pointer_path=pointer.pointer_path,
        active_model_path=pointer.active_package_path,
        previous_model_path=pointer.previous_package_path,
        active_model_id=pointer.active_package_id,
        active_model_sha256=pointer.active_package_sha256,
    )
