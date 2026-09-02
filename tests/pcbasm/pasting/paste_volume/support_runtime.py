"""Real PyTorch/ONNX Runtime artifacts shared by paste-volume runtime tests."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import attrs
import onnx
import onnxruntime as ort
import pytest
import torch

from pcbasm.pasting.paste_volume.compile_parity import (
    COMPILE_PARITY_KIND,
    COMPILE_PARITY_SCHEMA_VERSION,
    COMPILE_PARITY_TOLERANCE_PROFILE,
    CompileParityCaseResult,
    CompileParityConfig,
    CompileParityProvenance,
    CompileParityReport,
    CompileParityRuntime,
    validate_compile_parity_report,
)
from pcbasm.pasting.paste_volume.cross_validation import (
    CrossValidationFoldResult,
    CrossValidationResult,
    load_cross_validation_result,
)
from pcbasm.pasting.paste_volume.data import (
    build_sample_index,
    create_session_split,
    resolve_dataset_inputs,
    save_split_manifest,
)
from pcbasm.pasting.paste_volume.export import (
    ArtifactLineage,
    BenchmarkCategoryResult,
    CandidateEvaluation,
    CandidatePrediction,
    CrossValidationPromotionEvidence,
    ExportParityReport,
    ExportParitySampleResult,
    ExportParityShapeResult,
    FinalizedModelCandidate,
    ModelBenchmarkResult,
    ModelCandidateValidation,
    ModelPackageResult,
    build_model_candidate_validation,
    evaluate_frozen_test_candidate,
    evaluate_frozen_test_candidate_from_dataset,
    evaluate_validation_candidate,
    evaluate_validation_candidate_from_dataset,
    export_onnx_model_from_formal_weights,
    finalize_selected_model_candidate,
    promote_model_package_from_dataset,
    read_onnx_artifact_lineage,
    run_compile_parity_from_formal_weights,
    run_export_parity_from_formal_weights,
    select_model_candidate,
    validate_export_parity_report,
)
from pcbasm.pasting.paste_volume.formal_artifact import FormalArtifactAttestation
from pcbasm.pasting.paste_volume.model import (
    PasteVolumeModelConfig,
    PasteVolumeResNet,
)
from pcbasm.pasting.paste_volume.reporting import (
    Prediction,
    build_diagnostic_report,
    cross_group_fold_id,
)
from pcbasm.pasting.paste_volume.training import (
    FormalTrainingWeights,
    RegressionMetrics,
)
from tests.pcbasm.pasting.paste_volume.support_data import write_synthetic_session

_TEST_MODEL_MEAN_UL = float(torch.tensor(0.11, dtype=torch.float32).item())


def canonical_preprocess_schema() -> dict[str, object]:
    return {
        "schema_version": 1,
        "channel_order": "RGB",
        "input_channels": 6,
        "normalization": {
            "kind": "sample-layer-norm",
            "axes": [0, 1, 2],
            "affine": False,
            "per_channel": False,
            "epsilon": 1e-5,
        },
        "image_constraints": {
            "min_size": 32,
            "max_size": 1024,
            "max_pixels": 262144,
            "stride": 32,
            "normalization_epsilon": 1e-5,
        },
    }


def make_test_lineage(
    *,
    dataset_fingerprint: str = "sha256:" + "d" * 64,
    split_fingerprint: str = "sha256:" + "e" * 64,
    training_protocol_fingerprint: str = "sha256:" + "f" * 64,
    parent: bool = False,
) -> ArtifactLineage:
    return ArtifactLineage(
        source_run_id="run-123",
        source_checkpoint_sha256="sha256:" + "1" * 64,
        dataset_fingerprint=dataset_fingerprint,
        split_fingerprint=split_fingerprint,
        training_protocol_fingerprint=training_protocol_fingerprint,
        parent_run_id="base-run" if parent else None,
        parent_checkpoint_id="sha256:" + "2" * 64 if parent else None,
    )


def write_test_weights(path: Path, lineage: ArtifactLineage) -> Path:
    torch.manual_seed(7)
    config = PasteVolumeModelConfig(
        stem_channels=(8, 8, 8),
        stage_channels=(8, 8, 8),
        blocks_per_stage=(1, 1, 1),
        group_norm_groups=4,
        hidden_features=8,
    )
    model = PasteVolumeResNet(config)
    for parameter in model.parameters():
        torch.nn.init.zeros_(parameter)
    for name, parameter in model.named_parameters():
        if name == "_mean_head.bias":
            torch.nn.init.constant_(
                parameter, math.log(math.expm1(_TEST_MODEL_MEAN_UL))
            )
    torch.save(
        {
            "weight_schema_version": 1,
            "kind": "paste-volume-model-weights",
            "model_config": config.to_dict(),
            "state_dict": model.state_dict(),
            "preprocess_schema": canonical_preprocess_schema(),
            "uncertainty_log_variance_offset": 0.0,
            "run_kind": "finetune"
            if lineage.parent_run_id is not None
            else "base-train",
            **lineage.to_dict(),
        },
        path,
    )
    return path


def formal_test_weights(
    weights_path: Path, lineage: ArtifactLineage
) -> FormalTrainingWeights:
    """Unit fixture for a typed boundary; live verification has separate
    tests."""

    path = weights_path.resolve(strict=True)
    weights_sha256 = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    run_kind = "finetune" if lineage.parent_run_id is not None else "base-train"
    attestation = FormalArtifactAttestation(
        run_id=lineage.source_run_id,
        run_kind=run_kind,
        tracking_uri_sha256="sha256:" + "a" * 64,
        output_path=path,
        output_kind="file",
        output_fingerprint=weights_sha256,
    )
    return FormalTrainingWeights(
        weights_path=path,
        weights_sha256=weights_sha256,
        run_kind=cast(Any, run_kind),
        source_run_id=lineage.source_run_id,
        dataset_fingerprint=lineage.dataset_fingerprint,
        split_fingerprint=lineage.split_fingerprint,
        training_protocol_fingerprint=lineage.training_protocol_fingerprint,
        attestation=attestation,
    )


def export_test_model(directory: Path, lineage: ArtifactLineage) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    weights = write_test_weights(directory / "weights.pt", lineage)
    exported = export_onnx_model_from_formal_weights(
        formal_test_weights(weights, lineage),
        directory / "model.export.onnx",
        verification_shapes=((32, 32), (64, 96)),
    ).model_path
    candidate = directory / "model.onnx"
    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
    options.optimized_model_filepath = str(candidate)
    ort.InferenceSession(
        str(exported),
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )
    model = onnx.load(str(candidate), load_external_data=False)
    metadata_entry = next(
        entry
        for entry in model.metadata_props
        if entry.key == "pcbasm.paste_volume.export"
    )
    metadata = json.loads(metadata_entry.value)
    metadata["artifact_role"] = "optimized-fp32"
    metadata["source_export_sha256"] = _sha256(exported)
    metadata["artifact_provenance"] = {
        "role": "optimized-fp32",
        "tool": "onnxruntime",
        "version": ort.__version__,
    }
    metadata_entry.value = json.dumps(
        metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    onnx.save(model, str(candidate), save_as_external_data=False)
    return candidate


def passing_predictions(
    count: int = 10, *, sample_prefix: str = "validation"
) -> tuple[CandidatePrediction, ...]:
    mean = _TEST_MODEL_MEAN_UL
    return tuple(
        CandidatePrediction(
            sample_id=f"{sample_prefix}-sample-{index}",
            target_volume_ul=mean,
            mean_volume_ul=mean,
            log_variance_volume_ul2=0.0,
            reference_mean_volume_ul=mean,
            reference_log_variance_volume_ul2=0.0,
            sequence_group="session-a",
            sequence_index=index,
            padded_mean_volume_ul={
                "padding-10-top": mean,
                "padding-25-right": mean,
                "padding-50-bottom": mean,
            },
        )
        for index in range(count)
    )


def passing_benchmark(
    evaluation: CandidateEvaluation, model_path: Path
) -> ModelBenchmarkResult:
    lineage = ArtifactLineage(
        source_run_id=evaluation.source_run_id,
        source_checkpoint_sha256=evaluation.source_checkpoint_sha256,
        dataset_fingerprint=evaluation.training_dataset_fingerprint,
        split_fingerprint=evaluation.training_split_fingerprint,
        training_protocol_fingerprint=evaluation.training_protocol_fingerprint,
        parent_run_id=evaluation.parent_run_id,
        parent_checkpoint_id=evaluation.parent_checkpoint_id,
    )
    return ModelBenchmarkResult(
        schema_version=1,
        candidate_id=evaluation.candidate_id,
        model_artifact_sha256=evaluation.model_artifact_sha256,
        source_run_id=lineage.source_run_id,
        source_checkpoint_sha256=lineage.source_checkpoint_sha256,
        source_checkpoint_role=lineage.source_checkpoint_role,
        training_dataset_fingerprint=lineage.dataset_fingerprint,
        training_split_fingerprint=lineage.split_fingerprint,
        training_protocol_fingerprint=evaluation.training_protocol_fingerprint,
        dataset_fingerprint=evaluation.dataset_fingerprint,
        split_fingerprint=evaluation.split_fingerprint,
        benchmark_sample_ids=evaluation.evaluated_sample_ids[:5],
        parent_run_id=lineage.parent_run_id,
        parent_checkpoint_id=lineage.parent_checkpoint_id,
        platform_model="Raspberry Pi 5 Model B Rev 1.0",
        is_raspberry_pi_5=True,
        cold_latency_ms=150.0,
        p50_latency_ms=70.0,
        p95_latency_ms=94.0,
        p99_latency_ms=104.0,
        peak_rss_bytes=100_000_000,
        artifact_size_bytes=model_path.stat().st_size,
        sample_count=len(evaluation.evaluated_sample_ids[:5]),
        warmup_iterations=10,
        measured_iterations=100,
        category_results=tuple(
            BenchmarkCategoryResult(
                category=cast(Any, category),
                sample_id=evaluation.evaluated_sample_ids[index],
                image_height=height,
                image_width=width,
                warmup_iterations=10,
                measured_iterations=100,
                p50_latency_ms=70.0 + index,
                p95_latency_ms=90.0 + index,
                p99_latency_ms=100.0 + index,
            )
            for index, (category, height, width) in enumerate(
                (
                    ("small", 32, 32),
                    ("medium", 64, 64),
                    ("large", 128, 128),
                    ("portrait", 96, 64),
                    ("landscape", 64, 96),
                )
            )
        ),
        cold_start_clock="CLOCK_MONOTONIC",
        cold_start_origin="process-start:/proc/self/stat",
        os_id="raspios",
        os_release="bookworm",
        python_version="3.12.0",
        onnxruntime_version=ort.__version__,
        cpu_governor="performance",
        platform_machine="aarch64",
        power_condition="official 27W USB-C power supply",
        cooling_condition="active cooler",
    )


def passing_compile_parity(
    model_path: Path, lineage: ArtifactLineage
) -> CompileParityReport:
    """Release binding unit tests用のstrict typed compile evidence."""

    provenance = CompileParityProvenance(
        weights_sha256=_source_weights_sha256(model_path),
        training_protocol_fingerprint=lineage.training_protocol_fingerprint,
        dataset_fingerprint=lineage.dataset_fingerprint,
        split_fingerprint=lineage.split_fingerprint,
    )
    runtime = CompileParityRuntime(
        backend="inductor",
        mode="default",
        device="cpu",
        dtype="float32",
        torch_version=str(torch.__version__),
        tolerance_profile=COMPILE_PARITY_TOLERANCE_PROFILE,
        rtol=1e-3,
        atol=1e-5,
        fullgraph=False,
        dynamic=None,
        compile_setup_seconds=0.001,
        graph_break_count=0,
    )
    shapes = (
        ("minimum", (1, 6, 32, 32)),
        ("maximum-area", (1, 6, 512, 512)),
        ("portrait", (1, 6, 1024, 256)),
        ("landscape", (1, 6, 256, 1024)),
        ("training-batch", (1, 6, 64, 80)),
    )
    cases = tuple(
        CompileParityCaseResult(
            case_id=f"test-{shape_case}",
            shape_case=cast(Any, shape_case),
            input_shape=input_shape,
            sample_ids=(f"compile-{shape_case}",),
            compile_succeeded=True,
            parity_passed=True,
            trainable_parameter_count=1,
            checked_gradient_count=1,
            missing_gradient_parameters=(),
            nonfinite_gradient_parameters=(),
            mismatched_gradient_parameters=(),
            eager_step_seconds=0.001,
            compile_and_first_step_seconds=0.001,
            compiled_replay_step_seconds=0.001,
            eager_samples_per_second=1000.0,
            compiled_samples_per_second=1000.0,
            mean_max_abs_error=0.0,
            mean_max_relative_error=0.0,
            log_variance_max_abs_error=0.0,
            log_variance_max_relative_error=0.0,
            loss_max_abs_error=0.0,
            loss_max_relative_error=0.0,
            gradient_max_abs_error=0.0,
            gradient_max_relative_error=0.0,
            failure_reason=None,
        )
        for shape_case, input_shape in shapes
    )
    report = CompileParityReport(
        kind=COMPILE_PARITY_KIND,
        schema_version=COMPILE_PARITY_SCHEMA_VERSION,
        created_at_utc=datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
        success=True,
        provenance=provenance,
        runtime=runtime,
        cases=cases,
        content_sha256="",
    )
    payload = report.to_dict()
    del payload["content_sha256"]
    digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
    )
    finalized = replace(report, content_sha256=digest)
    validate_compile_parity_report(finalized)
    return finalized


def passing_cross_validation_reports(
    dataset_path: Path,
    lineage: ArtifactLineage,
    output_directory: Path,
) -> tuple[CrossValidationResult, ...]:
    """Promotion tests用の persisted strict 3-dimension evidence."""

    composite = resolve_dataset_inputs(roots=(dataset_path,))
    if composite.composite_fingerprint != lineage.dataset_fingerprint:
        raise ValueError("test cross-validation dataset lineage mismatch")
    samples = tuple(
        sorted(build_sample_index(composite), key=lambda item: item.sample_id)
    )
    if len(samples) < 2:
        raise ValueError("test cross-validation evidence requires at least two samples")
    predictions = tuple(
        Prediction(
            sample_id=sample.sample_id,
            mean_volume_ul=sample.measured_volume_ul,
            std_volume_ul=1.0,
        )
        for sample in samples
    )
    by_id = {sample.sample_id: sample for sample in samples}
    reports: list[CrossValidationResult] = []
    output_directory.mkdir(parents=True, exist_ok=True)
    for dimension in ("machine", "paste_lot", "nozzle"):
        folds: list[CrossValidationFoldResult] = []
        for group_index, group_name in enumerate(("group-a", "group-b")):
            held_out_ids = tuple(
                sample.sample_id
                for index, sample in enumerate(samples)
                if index % 2 == group_index
            )
            held_out_predictions = tuple(
                prediction
                for prediction in predictions
                if prediction.sample_id in set(held_out_ids)
            )
            held_out_samples = tuple(by_id[sample_id] for sample_id in held_out_ids)
            metrics = _passing_regression_metrics(len(held_out_ids))
            folds.append(
                CrossValidationFoldResult(
                    fold_id=cross_group_fold_id(cast(Any, dimension), group_name),
                    dimension=cast(Any, dimension),
                    held_out_group=group_name,
                    training_run_id=f"train-{dimension}-{group_name}",
                    evaluation_run_id=f"evaluate-{dimension}-{group_name}",
                    weights_path=output_directory / f"{dimension}-{group_name}.pt",
                    split_path=output_directory
                    / f"{dimension}-{group_name}.split.json",
                    evaluation_report_path=(
                        output_directory / f"{dimension}-{group_name}.evaluation.json"
                    ),
                    diagnostic_report_path=(
                        output_directory / f"{dimension}-{group_name}.diagnostics.json"
                    ),
                    weights_sha256="sha256:" + "1" * 64,
                    split_sha256="sha256:" + "2" * 64,
                    evaluation_report_sha256="sha256:" + "3" * 64,
                    diagnostic_report_sha256="sha256:" + "4" * 64,
                    split_fingerprint="sha256:" + str(group_index + 5) * 64,
                    held_out_sample_ids=held_out_ids,
                    training_best_validation_metrics=metrics,
                    held_out_metrics=metrics,
                    predictions=held_out_predictions,
                    diagnostics=build_diagnostic_report(
                        held_out_samples, held_out_predictions
                    ),
                )
            )
        sorted_folds = tuple(sorted(folds, key=lambda item: item.fold_id))
        diagnostics = build_diagnostic_report(samples, predictions)
        evidence = {
            "kind": "pcbasm-paste-volume-cross-validation-report",
            "schema_version": 1,
            "available": True,
            "reason": None,
            "dimension": dimension,
            "composite_fingerprint": lineage.dataset_fingerprint,
            "dataset_sample_ids": [sample.sample_id for sample in samples],
            "folds": [fold.evidence_dict() for fold in sorted_folds],
            "diagnostics": diagnostics.to_dict(),
            "protocol_fingerprint": lineage.training_protocol_fingerprint,
        }
        report_path = output_directory / f"cross-validation-{dimension}.json"
        report = CrossValidationResult(
            available=True,
            reason=None,
            dimension=cast(Any, dimension),
            composite_fingerprint=lineage.dataset_fingerprint,
            dataset_sample_ids=tuple(sample.sample_id for sample in samples),
            folds=sorted_folds,
            diagnostics=diagnostics,
            report_path=report_path,
            report_fingerprint="sha256:"
            + hashlib.sha256(
                json.dumps(
                    evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest(),
            protocol_fingerprint=lineage.training_protocol_fingerprint,
        )
        report_path.write_text(
            json.dumps(
                report.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )
        reports.append(load_cross_validation_result(report_path))
    return tuple(reports)


def passing_cross_validation_evidence(
    dataset_path: Path,
    lineage: ArtifactLineage,
    output_directory: Path,
) -> tuple[CrossValidationPromotionEvidence, ...]:
    """Promotion tests用のformal summary attestation付きevidence."""

    reports = passing_cross_validation_reports(dataset_path, lineage, output_directory)
    tracking_uri_sha256 = "sha256:" + "a" * 64
    return tuple(
        CrossValidationPromotionEvidence(
            report=report,
            summary_attestation=FormalArtifactAttestation(
                run_id=f"cross-validation-summary-{report.dimension}",
                run_kind="cross-validation-summary",
                tracking_uri_sha256=tracking_uri_sha256,
                output_path=report.report_path.resolve(),
                output_kind="file",
                output_fingerprint="sha256:" + _sha256(report.report_path),
            ),
        )
        for report in reports
    )


def _passing_regression_metrics(sample_count: int) -> RegressionMetrics:
    return RegressionMetrics(
        gaussian_nll=0.0,
        mae_ul=0.0,
        rmse_ul=0.0,
        normalized_error_mean=0.0,
        normalized_error_std=0.0,
        normalized_error_score=0.0,
        median_absolute_relative_error=0.0,
        p95_absolute_relative_error=0.0,
        one_std_coverage=1.0,
        mean_prediction_std_ul=1.0,
        invalid_prediction_count=0,
        sample_count=sample_count,
    )


def passing_export_parity(
    evaluation: CandidateEvaluation,
    model_path: Path,
) -> ExportParityReport:
    sample_results = tuple(
        ExportParitySampleResult(
            sample_id=sample_id,
            mean_absolute_error_ul=0.0,
            mean_tolerance_ul=1e-6,
            log_variance_absolute_error=0.0,
            log_variance_tolerance=1e-6,
            passed=True,
        )
        for sample_id in evaluation.evaluated_sample_ids
    )
    shape_results = tuple(
        ExportParityShapeResult(
            case_name=cast(Any, case_name),
            image_height=height,
            image_width=width,
            mean_absolute_error_ul=0.0,
            mean_tolerance_ul=1e-6,
            log_variance_absolute_error=0.0,
            log_variance_tolerance=1e-6,
            passed=True,
        )
        for case_name, height, width in (
            ("minimum", 32, 32),
            ("maximum-area", 512, 512),
            ("portrait", 1024, 256),
            ("landscape", 256, 1024),
        )
    )
    report = ExportParityReport(
        kind="pcbasm-paste-volume-export-parity",
        schema_version=1,
        weights_sha256=_source_weights_sha256(model_path),
        fp32_model_artifact_sha256=(evaluation.fp32_reference_artifact_sha256),
        training_protocol_fingerprint=(evaluation.training_protocol_fingerprint),
        dataset_fingerprint=evaluation.dataset_fingerprint,
        split_fingerprint=evaluation.split_fingerprint,
        split_name="validation",
        sample_ids=evaluation.evaluated_sample_ids,
        training_sample_ids=evaluation.training_sample_ids,
        sample_results=sample_results,
        shape_results=shape_results,
        maximum_mean_absolute_error_ul=0.0,
        maximum_log_variance_absolute_error=0.0,
        success=True,
        content_sha256="",
    )
    payload = report.to_dict()
    del payload["content_sha256"]
    content_sha256 = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
    )
    finalized = attrs.evolve(report, content_sha256=content_sha256)
    validate_export_parity_report(finalized)
    return finalized


def passing_candidate(
    model_path: Path,
    lineage: ArtifactLineage,
    compile_parity: CompileParityReport | None = None,
) -> ModelCandidateValidation:
    evaluation = evaluate_validation_candidate(
        model_path,
        model_format="onnx-fp32",
        predictions=passing_predictions(),
        lineage=lineage,
        fp32_reference_artifact_sha256=_source_export_sha256(model_path),
        evaluation_dataset_fingerprint=lineage.dataset_fingerprint,
        evaluation_split_fingerprint=lineage.split_fingerprint,
        training_sample_ids=("training-sample-0", "training-sample-1"),
    )
    return build_model_candidate_validation(
        evaluation,
        passing_benchmark(evaluation, model_path),
        compile_parity or passing_compile_parity(model_path, lineage),
        passing_export_parity(evaluation, model_path),
        model_path,
    )


def passing_finalized(
    model_path: Path,
    lineage: ArtifactLineage,
    compile_parity: CompileParityReport | None = None,
) -> FinalizedModelCandidate:
    selection = select_model_candidate(
        (passing_candidate(model_path, lineage, compile_parity),)
    )
    frozen = evaluate_frozen_test_candidate(
        selection,
        predictions=passing_predictions(sample_prefix="frozen-test"),
    )
    return finalize_selected_model_candidate(selection, frozen)


def promote_test_model(
    model_path: Path,
    lineage: ArtifactLineage,
    output: Path,
    *,
    version: str = "test-v1",
) -> ModelPackageResult:
    if read_onnx_artifact_lineage(model_path) != lineage:
        raise ValueError("test model lineageが指定値と不一致です")
    root = model_path.parent.parent
    dataset_path = root / "dataset"
    split_path = root / "split.json"
    finalized = _finalized_from_dataset(
        model_path,
        model_path.parent / "model.export.onnx",
        dataset_path,
        split_path,
    )
    cross_validation_evidence = passing_cross_validation_evidence(
        dataset_path, lineage, root / "cross-validation-evidence"
    )
    return promote_model_package_from_dataset(
        finalized,
        output,
        dataset_paths=(dataset_path,),
        split_manifest=split_path,
        cross_validation_evidence=cross_validation_evidence,
        model_name="paste-volume-test",
        model_version=version,
    )


@attrs.frozen
class RuntimeArtifacts:
    directory: Path
    lineage: ArtifactLineage
    model_path: Path
    fp32_reference_path: Path
    dataset_path: Path
    split_path: Path
    compile_parity: CompileParityReport
    export_parity: ExportParityReport
    cross_validation_reports: tuple[CrossValidationResult, ...]
    cross_validation_evidence: tuple[CrossValidationPromotionEvidence, ...]
    finalized: FinalizedModelCandidate
    package: ModelPackageResult


@pytest.fixture(scope="session")
def runtime_artifacts(tmp_path_factory: pytest.TempPathFactory) -> RuntimeArtifacts:
    directory = tmp_path_factory.mktemp("paste-volume-runtime")
    dataset_path = directory / "dataset"
    for index in range(3):
        write_synthetic_session(
            dataset_path,
            f"session-{index}",
            pad_count=1,
            views_per_pad=6,
            width=80,
            height=64,
        )
    composite = resolve_dataset_inputs(roots=(dataset_path,))
    samples = build_sample_index(composite)
    split = create_session_split(samples, composite.composite_fingerprint, seed=31)
    split_path = directory / "split.json"
    save_split_manifest(split, split_path)
    lineage = make_test_lineage(
        dataset_fingerprint=composite.composite_fingerprint,
        split_fingerprint=split.split_fingerprint,
        parent=True,
    )
    model_path = export_test_model(directory / "export", lineage)
    fp32_reference_path = directory / "export" / "model.export.onnx"
    formal_weights = formal_test_weights(directory / "export" / "weights.pt", lineage)
    diagnostic_compile_parity = run_compile_parity_from_formal_weights(
        formal_weights,
        (dataset_path,),
        split_path,
        config=CompileParityConfig(backend="eager", device="cpu"),
    )
    if not diagnostic_compile_parity.success:
        raise AssertionError("runtime fixture compile parity did not pass")
    compile_parity = passing_compile_parity(model_path, lineage)
    export_parity = run_export_parity_from_formal_weights(
        formal_weights,
        fp32_reference_path,
        (dataset_path,),
        split_path,
    )
    if not export_parity.success:
        raise AssertionError("runtime fixture export parity did not pass")
    finalized = _finalized_from_dataset(
        model_path,
        fp32_reference_path,
        dataset_path,
        split_path,
        compile_parity,
        export_parity,
    )
    cross_validation_evidence = passing_cross_validation_evidence(
        dataset_path, lineage, directory / "cross-validation-evidence"
    )
    cross_validation_reports = tuple(
        evidence.report for evidence in cross_validation_evidence
    )
    package = promote_model_package_from_dataset(
        finalized,
        directory / "promoted-v1",
        dataset_paths=(dataset_path,),
        split_manifest=split_path,
        cross_validation_evidence=cross_validation_evidence,
        model_name="paste-volume-test",
        model_version="test-v1",
    )
    return RuntimeArtifacts(
        directory=directory,
        lineage=lineage,
        model_path=model_path,
        fp32_reference_path=fp32_reference_path,
        dataset_path=dataset_path,
        split_path=split_path,
        compile_parity=compile_parity,
        export_parity=export_parity,
        cross_validation_reports=cross_validation_reports,
        cross_validation_evidence=cross_validation_evidence,
        finalized=finalized,
        package=package,
    )


def _finalized_from_dataset(
    model_path: Path,
    fp32_reference_path: Path,
    dataset_path: Path,
    split_path: Path,
    compile_parity: CompileParityReport | None = None,
    export_parity: ExportParityReport | None = None,
) -> FinalizedModelCandidate:
    validation = evaluate_validation_candidate_from_dataset(
        model_path,
        fp32_reference_path,
        model_format="onnx-fp32",
        dataset_paths=(dataset_path,),
        split_manifest=split_path,
    )
    selection = select_model_candidate(
        (
            build_model_candidate_validation(
                validation,
                passing_benchmark(validation, model_path),
                compile_parity
                or passing_compile_parity(
                    model_path, read_onnx_artifact_lineage(model_path)
                ),
                export_parity or passing_export_parity(validation, model_path),
                model_path,
            ),
        )
    )
    frozen = evaluate_frozen_test_candidate_from_dataset(
        selection,
        fp32_reference_path,
        dataset_paths=(dataset_path,),
        split_manifest=split_path,
    )
    return finalize_selected_model_candidate(selection, frozen)


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_export_sha256(path: Path) -> str:
    model = onnx.load(str(path), load_external_data=False)
    metadata_entry = next(
        entry
        for entry in model.metadata_props
        if entry.key == "pcbasm.paste_volume.export"
    )
    metadata = json.loads(metadata_entry.value)
    value = metadata.get("source_export_sha256")
    if not isinstance(value, str):
        raise AssertionError("test candidateにsource export hashがありません")
    return value


def _source_weights_sha256(path: Path) -> str:
    model = onnx.load(str(path), load_external_data=False)
    metadata_entry = next(
        entry
        for entry in model.metadata_props
        if entry.key == "pcbasm.paste_volume.export"
    )
    metadata = json.loads(metadata_entry.value)
    value = metadata.get("source_weights_sha256")
    if not isinstance(value, str):
        raise AssertionError("test candidateにsource weights hashがありません")
    return value
