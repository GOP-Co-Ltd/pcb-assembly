"""Candidate selection and release commands for paste-volume models."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ml.cli.tracked_operation import OperationOutput as _OperationOutput

from .support import (
    lineage_tags as _lineage_tags,
    require_formal_artifact as _require_formal_artifact,
    tracked_operation as _tracked_operation,
)

_FROZEN_TEST_RECEIPT_KIND = "pcbasm-paste-volume-frozen-test-consumption"
_FROZEN_TEST_RECEIPT_SCHEMA_VERSION = 1


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _write_json_create_only(path: Path, payload: Mapping[str, object]) -> Path:
    output = Path(path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{output.name}.",
            suffix=".new.tmp",
            dir=output.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(
                payload,
                stream,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, output)
        except FileExistsError as error:
            raise FileExistsError(
                f"create-only artifact already exists: {output}"
            ) from error
        directory_descriptor = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output


def _evaluation_metrics(evaluation: Any) -> dict[str, float]:
    return {
        "primary_accuracy_score": float(evaluation.primary_accuracy_score),
        "coverage_68": float(evaluation.coverage_68),
        "accepted_accuracy_score": float(evaluation.accepted_accuracy_score),
        "accepted_sample_coverage": float(evaluation.accepted_sample_coverage),
        "three_sample_acceptance": float(evaluation.three_sample_acceptance),
        "padding_accuracy_degradation": float(evaluation.padding_accuracy_degradation),
    }


def _evaluation_tags(evaluation: Any) -> dict[str, str | bool]:
    tags: dict[str, str | bool] = {
        "candidate_id": str(evaluation.candidate_id),
        "model_artifact_sha256": str(evaluation.model_artifact_sha256),
        "fp32_reference_artifact_sha256": str(
            evaluation.fp32_reference_artifact_sha256
        ),
        "source_run_id": str(evaluation.source_run_id),
        "source_checkpoint_sha256": str(evaluation.source_checkpoint_sha256),
        "training_dataset_fingerprint": str(evaluation.training_dataset_fingerprint),
        "training_split_fingerprint": str(evaluation.training_split_fingerprint),
        "dataset_fingerprint": str(evaluation.dataset_fingerprint),
        "split_fingerprint": str(evaluation.split_fingerprint),
        "gate_passed": bool(evaluation.gate_passed),
    }
    if evaluation.parent_run_id is not None:
        tags["parent_run_id"] = str(evaluation.parent_run_id)
    if evaluation.parent_checkpoint_id is not None:
        tags["parent_checkpoint_id"] = str(evaluation.parent_checkpoint_id)
    return tags


def _package_artifacts(package_path: Path) -> tuple[Path, ...]:
    names = (
        "manifest.json",
        "model.onnx",
        "preprocess.json",
        "evaluation.json",
        "SHA256SUMS",
    )
    return tuple(package_path / name for name in names)


def _frozen_test_receipt_path(selection: Path) -> Path:
    resolved = Path(selection).expanduser().resolve()
    return resolved.with_name(f"{resolved.name}.frozen-test-consumed.json")


def _frozen_test_data_binding(
    dataset_paths: Sequence[Path], split_manifest: Path
) -> dict[str, object]:
    from ml.paste_volume.data import (
        build_sample_index,
        load_split_manifest,
        resolve_dataset_inputs,
        validate_split_manifest,
    )

    paths = tuple(
        Path(path).expanduser().resolve(strict=True) for path in dataset_paths
    )
    if not paths:
        raise ValueError("frozen test datasetを1件以上指定してください")
    if len(paths) == 1 and paths[0].is_file():
        composite = resolve_dataset_inputs(manifest=paths[0])
    else:
        composite = resolve_dataset_inputs(roots=paths)
    samples = build_sample_index(composite)
    split_path = Path(split_manifest).expanduser().resolve(strict=True)
    split = load_split_manifest(
        split_path,
        expected_composite_fingerprint=composite.composite_fingerprint,
    )
    validate_split_manifest(
        split,
        samples,
        expected_composite_fingerprint=composite.composite_fingerprint,
    )
    return {
        "dataset_paths": [str(path) for path in paths],
        "dataset_fingerprint": composite.composite_fingerprint,
        "sample_index_fingerprint": split.sample_index_fingerprint,
        "split_manifest_path": str(split_path),
        "split_manifest_sha256": _sha256_file(split_path),
        "split_fingerprint": split.split_fingerprint,
    }


def _consume_frozen_test_once(
    *,
    selection: Path,
    dataset_binding: Mapping[str, object],
    output: Path,
    allow_external_split: bool,
    run_id: str,
) -> Path:
    selection_path = Path(selection).expanduser().resolve(strict=True)
    receipt_path = _frozen_test_receipt_path(selection_path)
    if receipt_path.exists():
        raise ValueError(
            "frozen test selectionは既に消費済みです。output pathを変えても再評価できません: "
            f"{receipt_path}"
        )
    payload = {
        "kind": _FROZEN_TEST_RECEIPT_KIND,
        "schema_version": _FROZEN_TEST_RECEIPT_SCHEMA_VERSION,
        "selection_path": str(selection_path),
        "selection_sha256": _sha256_file(selection_path),
        **dict(dataset_binding),
        "allow_external_split": allow_external_split,
        "intended_output_path": str(Path(output).expanduser().resolve()),
        "attempt_run_id": run_id,
        "retry_allowed": False,
    }
    try:
        return _write_json_create_only(receipt_path, payload)
    except FileExistsError as error:
        raise ValueError(
            "frozen test selectionは別processで消費されました。再評価できません: "
            f"{receipt_path}"
        ) from error


def candidate_evaluate(args: argparse.Namespace) -> Any:
    def action() -> _OperationOutput:
        from ml.paste_volume.release import (
            evaluate_validation_candidate_from_dataset,
            save_candidate_evaluation,
        )

        model_path = Path(args.model).expanduser().resolve(strict=True)
        reference_path = Path(args.fp32_reference).expanduser().resolve(strict=True)
        _require_formal_artifact(model_path, tracking_uri=args.tracking_uri)
        _require_formal_artifact(reference_path, tracking_uri=args.tracking_uri)
        split_manifest = Path(args.split_manifest).expanduser().resolve(strict=True)
        output_path = Path(args.output).expanduser().resolve()
        evaluation = evaluate_validation_candidate_from_dataset(
            model_path,
            reference_path,
            model_format=args.model_format,
            dataset_paths=tuple(
                Path(path).expanduser().resolve(strict=True) for path in args.data
            ),
            split_manifest=split_manifest,
        )
        save_candidate_evaluation(output_path, evaluation)
        return _OperationOutput(
            value=evaluation,
            artifacts=(split_manifest, output_path),
            metrics=_evaluation_metrics(evaluation),
            tags=_evaluation_tags(evaluation),
        )

    return _tracked_operation(
        args,
        run_kind="evaluate",
        action=action,
        initial_tags={"operation": "candidate-validation"},
    )


def candidate_package(args: argparse.Namespace) -> Any:
    def action() -> _OperationOutput:
        from ml.paste_volume.promotion import (
            package_model_candidate_from_dataset,
        )
        from ml.paste_volume.release import load_model_candidate_validation

        candidate_path = Path(args.candidate).expanduser().resolve(strict=True)
        _require_formal_artifact(candidate_path, tracking_uri=args.tracking_uri)
        split_manifest = Path(args.split_manifest).expanduser().resolve(strict=True)
        result = package_model_candidate_from_dataset(
            load_model_candidate_validation(candidate_path),
            Path(args.output).expanduser().resolve(),
            dataset_paths=tuple(
                Path(path).expanduser().resolve(strict=True) for path in args.data
            ),
            split_manifest=split_manifest,
            model_name=args.model_name,
            model_version=args.model_version,
        )
        return _OperationOutput(
            value=result,
            artifacts=(
                candidate_path,
                split_manifest,
                *_package_artifacts(result.package_path),
            ),
            metrics={
                "artifact_size_bytes": float(
                    (result.package_path / "model.onnx").stat().st_size
                )
            },
            tags={
                **_lineage_tags(result.lineage),
                "operation": "candidate-package",
                "model_id": result.model_id,
                "model_artifact_sha256": result.model_artifact_sha256,
                "manifest_sha256": result.manifest_sha256,
            },
        )

    return _tracked_operation(args, run_kind="export", action=action)


def benchmark(args: argparse.Namespace) -> Any:
    def action() -> _OperationOutput:
        from ml.paste_volume.artifact import ArtifactLineage
        from ml.paste_volume.benchmark import (
            benchmark_model_candidate_from_dataset,
            save_model_benchmark_result,
        )

        model_path = Path(args.model).expanduser().resolve(strict=True)
        if model_path.is_dir():
            model_path = (model_path / "model.onnx").resolve(strict=True)
        _require_formal_artifact(model_path, tracking_uri=args.tracking_uri)
        split_manifest = Path(args.split_manifest).expanduser().resolve(strict=True)
        output_path = Path(args.output).expanduser().resolve()
        result = benchmark_model_candidate_from_dataset(
            model_path,
            model_format=args.model_format,
            dataset_paths=tuple(
                Path(path).expanduser().resolve(strict=True) for path in args.data
            ),
            split_manifest=split_manifest,
            warmup_iterations=args.warmup_iterations,
            measured_iterations=args.measured_iterations,
            power_condition=args.power_condition,
            cooling_condition=args.cooling_condition,
        )
        save_model_benchmark_result(output_path, result)
        lineage = ArtifactLineage(
            source_run_id=result.source_run_id,
            source_checkpoint_sha256=result.source_checkpoint_sha256,
            dataset_fingerprint=result.training_dataset_fingerprint,
            split_fingerprint=result.training_split_fingerprint,
            training_protocol_fingerprint=result.training_protocol_fingerprint,
            parent_run_id=result.parent_run_id,
            parent_checkpoint_id=result.parent_checkpoint_id,
        )
        return _OperationOutput(
            value=result,
            artifacts=(split_manifest, output_path),
            metrics={
                "cold_latency_ms": result.cold_latency_ms,
                "p50_latency_ms": result.p50_latency_ms,
                "p95_latency_ms": result.p95_latency_ms,
                "p99_latency_ms": result.p99_latency_ms,
                "peak_rss_bytes": float(result.peak_rss_bytes),
                "artifact_size_bytes": float(result.artifact_size_bytes),
                **{
                    f"category.{category.category}.{metric}": float(
                        getattr(category, metric)
                    )
                    for category in result.category_results
                    for metric in (
                        "p50_latency_ms",
                        "p95_latency_ms",
                        "p99_latency_ms",
                    )
                },
            },
            tags={
                **_lineage_tags(lineage),
                "training_dataset_fingerprint": result.training_dataset_fingerprint,
                "training_split_fingerprint": result.training_split_fingerprint,
                "dataset_fingerprint": result.dataset_fingerprint,
                "split_fingerprint": result.split_fingerprint,
                "candidate_id": result.candidate_id,
                "model_artifact_sha256": result.model_artifact_sha256,
                "is_raspberry_pi_5": result.is_raspberry_pi_5,
                "platform_model": result.platform_model,
                "platform_machine": result.platform_machine,
                "os_id": result.os_id,
                "os_release": result.os_release,
                "python_version": result.python_version,
                "onnxruntime_version": result.onnxruntime_version,
                "cpu_governor": result.cpu_governor,
                "cold_start_clock": result.cold_start_clock,
                "cold_start_origin": result.cold_start_origin,
                "power_condition": result.power_condition,
                "cooling_condition": result.cooling_condition,
                "gate_passed": result.gate_passed,
            },
        )

    return _tracked_operation(args, run_kind="benchmark", action=action)


def candidate_bind(args: argparse.Namespace) -> Any:
    def action() -> _OperationOutput:
        from ml.paste_volume.benchmark import load_model_benchmark_result
        from ml.paste_volume.compile_parity import (
            load_compile_parity_report,
        )
        from ml.paste_volume.onnx import load_export_parity_report
        from ml.paste_volume.release import (
            build_model_candidate_validation,
            load_candidate_evaluation,
            save_model_candidate_validation,
        )

        compile_parity_path = (
            Path(args.compile_parity).expanduser().resolve(strict=True)
        )
        _require_formal_artifact(
            compile_parity_path,
            tracking_uri=args.tracking_uri,
            expected_run_kind="compile-parity",
        )
        export_parity_path = Path(args.export_parity).expanduser().resolve(strict=True)
        _require_formal_artifact(
            export_parity_path,
            tracking_uri=args.tracking_uri,
            expected_run_kind="export-parity",
        )
        evaluation_path = Path(args.evaluation).expanduser().resolve(strict=True)
        benchmark_path = Path(args.benchmark).expanduser().resolve(strict=True)
        model_path = Path(args.model).expanduser().resolve(strict=True)
        _require_formal_artifact(evaluation_path, tracking_uri=args.tracking_uri)
        _require_formal_artifact(benchmark_path, tracking_uri=args.tracking_uri)
        _require_formal_artifact(model_path, tracking_uri=args.tracking_uri)
        output_path = Path(args.output).expanduser().resolve()
        candidate = build_model_candidate_validation(
            load_candidate_evaluation(evaluation_path),
            load_model_benchmark_result(benchmark_path),
            load_compile_parity_report(compile_parity_path),
            load_export_parity_report(export_parity_path),
            model_path,
            dependency_complexity_rank=args.dependency_complexity_rank,
        )
        save_model_candidate_validation(output_path, candidate)
        return _OperationOutput(
            value=candidate,
            artifacts=(
                evaluation_path,
                benchmark_path,
                compile_parity_path,
                export_parity_path,
                output_path,
            ),
            metrics={
                **_evaluation_metrics(candidate.evaluation),
                "p95_latency_ms": candidate.benchmark.p95_latency_ms,
            },
            tags=_evaluation_tags(candidate.evaluation),
        )

    return _tracked_operation(
        args,
        run_kind="evaluate",
        action=action,
        initial_tags={"operation": "candidate-bind"},
    )


def candidate_select(args: argparse.Namespace) -> Any:
    def action() -> _OperationOutput:
        from ml.paste_volume.release import (
            load_model_candidate_validation,
            save_selected_model_candidate,
            select_model_candidate,
        )

        candidate_paths = tuple(
            Path(path).expanduser().resolve(strict=True) for path in args.candidates
        )
        for candidate_path in candidate_paths:
            _require_formal_artifact(candidate_path, tracking_uri=args.tracking_uri)
        candidates = tuple(
            load_model_candidate_validation(candidate_path)
            for candidate_path in candidate_paths
        )
        selection = select_model_candidate(candidates)
        output_path = Path(args.output).expanduser().resolve()
        save_selected_model_candidate(output_path, selection)
        evaluation = selection.candidate.evaluation
        return _OperationOutput(
            value=selection,
            artifacts=(
                *(candidate_path for candidate_path in candidate_paths),
                output_path,
            ),
            metrics={
                **_evaluation_metrics(evaluation),
                "p95_latency_ms": selection.candidate.benchmark.p95_latency_ms,
            },
            tags={
                **_evaluation_tags(evaluation),
                "compared_candidate_ids": ",".join(selection.compared_candidate_ids),
            },
        )

    return _tracked_operation(
        args,
        run_kind="evaluate",
        action=action,
        initial_tags={"operation": "candidate-select"},
    )


def candidate_frozen_test(args: argparse.Namespace) -> Any:
    def action() -> _OperationOutput:
        from ml.artifacts.formal import formal_artifact_attestation_path
        from ml.paste_volume.release import (
            evaluate_frozen_test_candidate_from_dataset,
            load_selected_model_candidate,
            save_candidate_evaluation,
        )

        selection_path = Path(args.selection).expanduser().resolve(strict=True)
        reference_path = Path(args.fp32_reference).expanduser().resolve(strict=True)
        split_manifest = Path(args.split_manifest).expanduser().resolve(strict=True)
        output_path = Path(args.output).expanduser().resolve()
        receipt_path = _frozen_test_receipt_path(selection_path)
        if receipt_path.exists():
            raise ValueError(
                "frozen test selectionは既に消費済みです。output pathを変えても再評価できません: "
                f"{receipt_path}"
            )
        _require_formal_artifact(selection_path, tracking_uri=args.tracking_uri)
        _require_formal_artifact(reference_path, tracking_uri=args.tracking_uri)
        if (
            output_path.exists()
            or formal_artifact_attestation_path(output_path).exists()
        ):
            raise FileExistsError(f"frozen test outputが既に存在します: {output_path}")
        dataset_paths = tuple(
            Path(path).expanduser().resolve(strict=True) for path in args.data
        )
        dataset_binding = _frozen_test_data_binding(dataset_paths, split_manifest)
        receipt_path = _consume_frozen_test_once(
            selection=selection_path,
            dataset_binding=dataset_binding,
            output=output_path,
            allow_external_split=args.allow_external_split,
            run_id=str(args._operation_run_id),
        )
        setattr(args, "_operation_failure_artifacts", (receipt_path,))
        selection = load_selected_model_candidate(selection_path)
        frozen_test = evaluate_frozen_test_candidate_from_dataset(
            selection,
            reference_path,
            dataset_paths=dataset_paths,
            split_manifest=split_manifest,
            allow_external_split=args.allow_external_split,
        )
        save_candidate_evaluation(output_path, frozen_test)
        return _OperationOutput(
            value=frozen_test,
            artifacts=(selection_path, split_manifest, receipt_path, output_path),
            metrics=_evaluation_metrics(frozen_test),
            tags=_evaluation_tags(frozen_test),
        )

    return _tracked_operation(
        args,
        run_kind="evaluate",
        action=action,
        initial_tags={"operation": "candidate-frozen-test"},
    )


def candidate_finalize(args: argparse.Namespace) -> Any:
    def action() -> _OperationOutput:
        from ml.paste_volume.release import (
            finalize_selected_model_candidate,
            load_candidate_evaluation,
            load_selected_model_candidate,
            save_finalized_model_candidate,
        )

        selection_path = Path(args.selection).expanduser().resolve(strict=True)
        frozen_test_path = Path(args.frozen_test).expanduser().resolve(strict=True)
        _require_formal_artifact(selection_path, tracking_uri=args.tracking_uri)
        _require_formal_artifact(frozen_test_path, tracking_uri=args.tracking_uri)
        finalized = finalize_selected_model_candidate(
            load_selected_model_candidate(selection_path),
            load_candidate_evaluation(frozen_test_path),
        )
        output_path = Path(args.output).expanduser().resolve()
        save_finalized_model_candidate(output_path, finalized)
        evaluation = finalized.frozen_test
        return _OperationOutput(
            value=finalized,
            artifacts=(selection_path, frozen_test_path, output_path),
            metrics=_evaluation_metrics(evaluation),
            tags=_evaluation_tags(evaluation),
        )

    return _tracked_operation(
        args,
        run_kind="evaluate",
        action=action,
        initial_tags={"operation": "candidate-finalize"},
    )


def promote(args: argparse.Namespace) -> Any:
    if (args.frozen_test_data is None) != (args.frozen_test_split_manifest is None):
        raise ValueError(
            "external frozen testの--frozen-test-dataと"
            "--frozen-test-split-manifestは両方指定してください"
        )
    if len(args.cross_validation) != 3:
        raise ValueError(
            "promotionにはmachine/paste_lot/nozzleの"
            "--cross-validation reportを各1件、合計3件指定してください"
        )

    def action() -> _OperationOutput:
        from ml.paste_volume.promotion import (
            load_formal_cross_validation_evidence,
            promote_model_package_from_dataset,
        )
        from ml.paste_volume.release import load_finalized_model_candidate

        finalized_path = Path(args.finalized).expanduser().resolve(strict=True)
        _require_formal_artifact(finalized_path, tracking_uri=args.tracking_uri)
        finalized = load_finalized_model_candidate(finalized_path)
        cross_validation_paths = tuple(
            Path(path).expanduser().resolve(strict=True)
            for path in args.cross_validation
        )
        cross_validation_evidence = tuple(
            load_formal_cross_validation_evidence(
                report_path,
                tracking_uri=args.tracking_uri,
            )
            for report_path in cross_validation_paths
        )
        split_manifest = Path(args.split_manifest).expanduser().resolve(strict=True)
        frozen_test_dataset_paths = (
            None
            if args.frozen_test_data is None
            else tuple(
                Path(path).expanduser().resolve(strict=True)
                for path in args.frozen_test_data
            )
        )
        frozen_test_split_manifest = (
            None
            if args.frozen_test_split_manifest is None
            else Path(args.frozen_test_split_manifest).expanduser().resolve(strict=True)
        )
        result = promote_model_package_from_dataset(
            finalized,
            Path(args.output).expanduser().resolve(),
            dataset_paths=tuple(
                Path(path).expanduser().resolve(strict=True) for path in args.data
            ),
            split_manifest=split_manifest,
            frozen_test_dataset_paths=frozen_test_dataset_paths,
            frozen_test_split_manifest=frozen_test_split_manifest,
            cross_validation_evidence=cross_validation_evidence,
            model_name=args.model_name,
            model_version=args.model_version,
        )
        evaluation = finalized.frozen_test
        return _OperationOutput(
            value=result,
            artifacts=(
                finalized_path,
                split_manifest,
                *cross_validation_paths,
                *(
                    ()
                    if frozen_test_split_manifest is None
                    else (frozen_test_split_manifest,)
                ),
                *_package_artifacts(result.package_path),
            ),
            metrics={
                **_evaluation_metrics(evaluation),
                "p95_latency_ms": (
                    finalized.selection.candidate.benchmark.p95_latency_ms
                ),
                "artifact_size_bytes": float(
                    (result.package_path / "model.onnx").stat().st_size
                ),
            },
            tags={
                **_evaluation_tags(evaluation),
                "model_id": result.model_id,
                "manifest_sha256": result.manifest_sha256,
            },
        )

    return _tracked_operation(args, run_kind="promote", action=action)
