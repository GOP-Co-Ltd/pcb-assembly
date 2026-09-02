"""Artifact and runtime commands for the paste-volume ML workflow."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from ml.cli.tracked_operation import OperationOutput as _OperationOutput

from .support import (
    lineage_tags as _lineage_tags,
    load_formal_training_weights as _load_formal_training_weights,
    require_formal_artifact as _require_formal_artifact,
    tracked_operation as _tracked_operation,
    write_report as _write_report,
)


def export(args: argparse.Namespace) -> Any:
    def action() -> _OperationOutput:
        from ml.paste_volume.onnx import (
            export_onnx_model_from_formal_weights,
        )

        formal_weights = _load_formal_training_weights(
            args.weights,
            tracking_uri=args.tracking_uri,
        )
        output_directory = Path(args.output).expanduser().resolve()
        model_path = output_directory / "model.fp32.onnx"
        result = export_onnx_model_from_formal_weights(
            formal_weights,
            model_path,
            opset_version=args.opset_version,
        )
        report = _write_report(output_directory / "export-report.json", result)
        return _OperationOutput(
            value=result,
            artifacts=(result.model_path, report),
            metrics={
                "artifact_size_bytes": float(result.model_path.stat().st_size),
            },
            tags={
                **_lineage_tags(result.lineage),
                "source_weights_sha256": formal_weights.weights_sha256,
                "model_artifact_sha256": result.model_artifact_sha256,
            },
        )

    return _tracked_operation(args, run_kind="export", action=action)


def optimize(args: argparse.Namespace) -> Any:
    def action() -> _OperationOutput:
        from ml.paste_volume.onnx import optimize_model

        source = Path(args.onnx_model).expanduser().resolve(strict=True)
        _require_formal_artifact(source, tracking_uri=args.tracking_uri)
        output_directory = Path(args.output).expanduser().resolve()
        split_manifest = Path(args.split_manifest).expanduser().resolve(strict=True)
        calibration_data = tuple(
            Path(path).expanduser().resolve(strict=True)
            for path in args.calibration_data
        )
        result = optimize_model(
            source,
            output_directory,
            calibration_data=calibration_data,
            split_manifest=split_manifest,
            calibration_sample_limit=args.calibration_sample_limit,
            seed=args.seed,
        )
        report = _write_report(output_directory / "optimization-report.json", result)
        return _OperationOutput(
            value=result,
            artifacts=(
                result.optimized_fp32_path,
                result.int8_path,
                report,
                split_manifest,
            ),
            metrics={
                "optimized_fp32_size_bytes": float(
                    result.optimized_fp32_path.stat().st_size
                ),
                "int8_size_bytes": float(result.int8_path.stat().st_size),
                "calibration_sample_count": float(len(result.calibration_sample_ids)),
                "calibration_session_count": float(len(result.calibration_session_ids)),
            },
            tags={
                **_lineage_tags(result.lineage),
                "source_model_artifact_sha256": (result.source_model_artifact_sha256),
                "optimized_fp32_artifact_sha256": (
                    result.optimized_fp32_artifact_sha256
                ),
                "int8_artifact_sha256": result.int8_artifact_sha256,
                "calibration_sample_index_fingerprint": (
                    result.calibration_sample_index_fingerprint
                ),
            },
        )

    return _tracked_operation(args, run_kind="optimize", action=action)


def _compile_parity_report_path(output: Path) -> Path:
    resolved = Path(output).expanduser().resolve()
    if resolved.exists():
        return resolved / "compile-parity.json" if resolved.is_dir() else resolved
    if resolved.suffix.lower() == ".json":
        return resolved
    return resolved / "compile-parity.json"


def _compile_parity_metrics(report: Any) -> dict[str, float]:
    metrics = {
        "compile_setup_seconds": float(report.runtime.compile_setup_seconds),
        "graph_break_count": float(report.runtime.graph_break_count),
        "case_count": float(len(report.cases)),
        "passing_case_count": float(sum(case.parity_passed for case in report.cases)),
        "compiled_case_count": float(
            sum(case.compile_succeeded for case in report.cases)
        ),
    }
    numeric_fields = (
        "trainable_parameter_count",
        "checked_gradient_count",
        "eager_step_seconds",
        "compile_and_first_step_seconds",
        "compiled_replay_step_seconds",
        "eager_samples_per_second",
        "compiled_samples_per_second",
        "mean_max_abs_error",
        "mean_max_relative_error",
        "log_variance_max_abs_error",
        "log_variance_max_relative_error",
        "loss_max_abs_error",
        "loss_max_relative_error",
        "gradient_max_abs_error",
        "gradient_max_relative_error",
    )
    for case in report.cases:
        prefix = f"case.{case.shape_case.replace('-', '_')}"
        metrics[f"{prefix}.compile_succeeded"] = float(case.compile_succeeded)
        metrics[f"{prefix}.parity_passed"] = float(case.parity_passed)
        for name in numeric_fields:
            value = getattr(case, name)
            if value is not None:
                metrics[f"{prefix}.{name}"] = float(value)
    return metrics


def _compile_parity_tags(report: Any) -> dict[str, str | bool]:
    failed_case_ids = tuple(
        case.case_id for case in report.cases if not case.parity_passed
    )
    return {
        "operation": "compile-parity",
        "compile_parity_success": bool(report.success),
        "compile_parity_sha256": str(report.content_sha256),
        "weights_sha256": str(report.provenance.weights_sha256),
        "training_protocol_fingerprint": str(
            report.provenance.training_protocol_fingerprint
        ),
        "dataset_fingerprint": str(report.provenance.dataset_fingerprint),
        "split_fingerprint": str(report.provenance.split_fingerprint),
        "compile_backend": str(report.runtime.backend),
        "compile_mode": str(report.runtime.mode),
        "compile_device": str(report.runtime.device),
        "compile_dtype": str(report.runtime.dtype),
        "compile_tolerance_profile": str(report.runtime.tolerance_profile),
        "compile_graph_break_count": str(report.runtime.graph_break_count),
        "failed_compile_parity_case_ids": ",".join(failed_case_ids),
    }


def compile_parity(args: argparse.Namespace) -> Any:
    def action() -> _OperationOutput:
        from ml.paste_volume.compile_parity import (
            CompileParityConfig,
            write_compile_parity_report,
        )
        from ml.paste_volume.release import (
            run_compile_parity_from_formal_weights,
        )

        formal_weights = _load_formal_training_weights(
            args.weights,
            tracking_uri=args.tracking_uri,
        )
        dataset_paths = tuple(
            Path(path).expanduser().resolve(strict=True) for path in args.data
        )
        split_manifest = Path(args.split_manifest).expanduser().resolve(strict=True)
        report = run_compile_parity_from_formal_weights(
            formal_weights,
            dataset_paths,
            split_manifest,
            config=CompileParityConfig(
                backend=args.backend,
                mode=args.mode,
                device=args.device,
            ),
        )
        report_path = _compile_parity_report_path(Path(args.output))
        write_compile_parity_report(report, report_path)
        return _OperationOutput(
            value=report,
            artifacts=(split_manifest, report_path),
            metrics=_compile_parity_metrics(report),
            tags=_compile_parity_tags(report),
            failure_message=(
                None
                if report.success
                else "torch.compile parity check failed; report was preserved"
            ),
        )

    return _tracked_operation(
        args,
        run_kind="compile-parity",
        action=action,
        initial_tags={"operation": "compile-parity"},
    )


def _export_parity_report_path(output: Path) -> Path:
    resolved = Path(output).expanduser().resolve()
    if resolved.exists():
        return resolved / "export-parity.json" if resolved.is_dir() else resolved
    if resolved.suffix.lower() == ".json":
        return resolved
    return resolved / "export-parity.json"


def _export_parity_metrics(report: Any) -> dict[str, float]:
    metrics = {
        "sample_count": float(len(report.sample_results)),
        "passing_sample_count": float(
            sum(sample.passed for sample in report.sample_results)
        ),
        "shape_case_count": float(len(report.shape_results)),
        "passing_shape_case_count": float(
            sum(shape.passed for shape in report.shape_results)
        ),
        "maximum_mean_absolute_error_ul": float(report.maximum_mean_absolute_error_ul),
        "maximum_log_variance_absolute_error": float(
            report.maximum_log_variance_absolute_error
        ),
    }
    for index, sample in enumerate(report.sample_results):
        prefix = f"sample.{index}"
        metrics[f"{prefix}.passed"] = float(sample.passed)
        metrics[f"{prefix}.mean_absolute_error_ul"] = float(
            sample.mean_absolute_error_ul
        )
        metrics[f"{prefix}.mean_tolerance_ul"] = float(sample.mean_tolerance_ul)
        metrics[f"{prefix}.log_variance_absolute_error"] = float(
            sample.log_variance_absolute_error
        )
        metrics[f"{prefix}.log_variance_tolerance"] = float(
            sample.log_variance_tolerance
        )
    for shape in report.shape_results:
        prefix = f"shape.{shape.case_name.replace('-', '_')}"
        metrics[f"{prefix}.passed"] = float(shape.passed)
        metrics[f"{prefix}.image_height"] = float(shape.image_height)
        metrics[f"{prefix}.image_width"] = float(shape.image_width)
        metrics[f"{prefix}.mean_absolute_error_ul"] = float(
            shape.mean_absolute_error_ul
        )
        metrics[f"{prefix}.mean_tolerance_ul"] = float(shape.mean_tolerance_ul)
        metrics[f"{prefix}.log_variance_absolute_error"] = float(
            shape.log_variance_absolute_error
        )
        metrics[f"{prefix}.log_variance_tolerance"] = float(
            shape.log_variance_tolerance
        )
    return metrics


def _export_parity_tags(report: Any) -> dict[str, str | bool]:
    failed_sample_ids = tuple(
        sample.sample_id for sample in report.sample_results if not sample.passed
    )
    failed_shape_cases = tuple(
        shape.case_name for shape in report.shape_results if not shape.passed
    )
    return {
        "operation": "export-parity",
        "export_parity_success": bool(report.success),
        "export_parity_sha256": str(report.content_sha256),
        "weights_sha256": str(report.weights_sha256),
        "fp32_model_artifact_sha256": str(report.fp32_model_artifact_sha256),
        "training_protocol_fingerprint": str(report.training_protocol_fingerprint),
        "dataset_fingerprint": str(report.dataset_fingerprint),
        "split_fingerprint": str(report.split_fingerprint),
        "split_name": str(report.split_name),
        "failed_export_parity_sample_ids": ",".join(failed_sample_ids),
        "failed_export_parity_shape_cases": ",".join(failed_shape_cases),
    }


def export_parity(args: argparse.Namespace) -> Any:
    def action() -> _OperationOutput:
        from ml.paste_volume.onnx import (
            run_export_parity_from_formal_weights,
            save_export_parity_report,
        )

        formal_weights = _load_formal_training_weights(
            args.weights,
            tracking_uri=args.tracking_uri,
        )
        fp32_model_path = Path(args.fp32_model).expanduser().resolve(strict=True)
        _require_formal_artifact(
            fp32_model_path,
            tracking_uri=args.tracking_uri,
            expected_run_kind="export",
        )
        dataset_paths = tuple(
            Path(path).expanduser().resolve(strict=True) for path in args.data
        )
        split_manifest = Path(args.split_manifest).expanduser().resolve(strict=True)
        report = run_export_parity_from_formal_weights(
            formal_weights,
            fp32_model_path,
            dataset_paths,
            split_manifest,
        )
        report_path = _export_parity_report_path(Path(args.output))
        save_export_parity_report(report_path, report)
        return _OperationOutput(
            value=report,
            artifacts=(
                formal_weights.weights_path,
                fp32_model_path,
                split_manifest,
                report_path,
            ),
            metrics=_export_parity_metrics(report),
            tags=_export_parity_tags(report),
            failure_message=(
                None
                if report.success
                else "FP32 ONNX export parity check failed; report was preserved"
            ),
        )

    return _tracked_operation(
        args,
        run_kind="export-parity",
        action=action,
        initial_tags={"operation": "export-parity"},
    )


def _decode_rgb_image(path: str) -> Any:
    import numpy as np
    import torch
    from torchvision.io import ImageReadMode, decode_image, read_file

    image_path = Path(path).expanduser().resolve(strict=True)
    encoded = read_file(str(image_path))
    if bytes(encoded[:8].tolist()) != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"lossless RGB PNGが必要です: {image_path}")
    decoded = decode_image(encoded, mode=ImageReadMode.UNCHANGED)
    if decoded.dtype != torch.uint8 or decoded.ndim != 3 or decoded.shape[0] != 3:
        raise ValueError(f"8-bit 3-channel RGB PNGが必要です: {image_path}")
    return np.asarray(decoded.permute(1, 2, 0).contiguous().numpy()).copy()


def infer(args: argparse.Namespace) -> Any:
    from ml.paste_volume.infer import load_paste_volume_estimator

    model_package = Path(args.model_package).expanduser().resolve(strict=True)
    estimator = load_paste_volume_estimator(model_package)
    prediction = estimator.predict(
        _decode_rgb_image(args.pre_image),
        _decode_rgb_image(args.post_image),
        pixel_per_mm=args.pixel_per_mm,
    )
    return {"model": estimator.model_info, "prediction": prediction}


def activate(args: argparse.Namespace) -> Any:
    from ml.paste_volume.promotion import activate_model_package

    model_package = Path(args.model_package).expanduser().resolve(strict=True)
    pointer = Path(args.pointer).expanduser().resolve()
    if pointer.is_relative_to(model_package):
        raise ValueError(
            "active model pointerはimmutable model packageの外に配置してください"
        )
    return activate_model_package(
        model_package,
        pointer,
    )


def rollback(args: argparse.Namespace) -> Any:
    from ml.paste_volume.promotion import rollback_active_model

    return rollback_active_model(Path(args.pointer).expanduser().resolve(strict=True))
