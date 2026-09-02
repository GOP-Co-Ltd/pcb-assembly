"""Hydra-independent operational CLI for paste-volume ML workflows."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from pcbasm.pasting.paste_volume.formal_artifact import (
    formal_artifact_attestation_path,
    publish_formal_artifact,
    verify_formal_artifact,
)

_DEFAULT_TRACKING_URI = "http://127.0.0.1:5000"
_DEFAULT_EXPERIMENT_NAME = "paste-volume"
_FROZEN_TEST_RECEIPT_KIND = "pcbasm-paste-volume-frozen-test-consumption"
_FROZEN_TEST_RECEIPT_SCHEMA_VERSION = 1
_DEPENDENCY_GROUP_BY_MODULE = {
    "PIL": "ml-runtime",
    "hydra": "ml-train",
    "hydra_plugins": "ml-hpo",
    "mlflow": "ml-train",
    "omegaconf": "ml-train",
    "onnx": "ml-export",
    "onnxruntime": "ml-runtime",
    "onnxscript": "ml-export",
    "optuna": "ml-hpo",
    "torch": "ml-runtime",
    "torchvision": "ml-runtime",
}


@dataclass(frozen=True)
class _OperationOutput:
    value: Any
    artifacts: tuple[Path, ...] = ()
    metrics: Mapping[str, float] = field(default_factory=dict)
    tags: Mapping[str, str | int | float | bool] = field(default_factory=dict)
    failure_message: str | None = None


def _jsonable(value: Any) -> Any:
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _jsonable(to_dict())
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if hasattr(type(value), "__attrs_attrs__"):
        import attrs

        return _jsonable(attrs.asdict(value))
    return value


def _print_json(value: Any) -> None:
    print(json.dumps(_jsonable(value), allow_nan=False, indent=2, sort_keys=True))


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


def _require_formal_artifact(
    artifact: Path,
    *,
    tracking_uri: str,
    expected_run_kind: str | None = None,
) -> None:
    verify_formal_artifact(
        artifact,
        tracking_uri=tracking_uri,
        expected_run_kind=expected_run_kind,
    )


def _resolved_arguments(args: argparse.Namespace) -> dict[str, Any]:
    from pcbasm.pasting.paste_volume.train import (
        sanitize_persisted_uri,
        sanitize_persisted_value,
    )

    resolved: dict[str, Any] = {}
    for key, value in vars(args).items():
        if key == "handler" or key.startswith("_"):
            continue
        if key == "tracking_uri":
            resolved[key] = sanitize_persisted_uri(str(value))
        else:
            resolved[key] = _jsonable(value)
    return cast(dict[str, Any], sanitize_persisted_value(resolved))


def _tracking_uri_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--tracking-uri",
        default=os.environ.get("MLFLOW_TRACKING_URI", _DEFAULT_TRACKING_URI),
        help="required MLflow tracking server URI",
    )


def _tracking_arguments(parser: argparse.ArgumentParser) -> None:
    _tracking_uri_argument(parser)
    parser.add_argument(
        "--experiment-name",
        default=_DEFAULT_EXPERIMENT_NAME,
        help=f"MLflow experiment name (default: {_DEFAULT_EXPERIMENT_NAME})",
    )
    parser.add_argument("--run-name")


def _require_tracking_server_uri(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("MLflow tracking URI must be an HTTP(S) tracking server")
    return value


def _tracked_operation(
    args: argparse.Namespace,
    *,
    run_kind: str,
    action: Callable[[], _OperationOutput],
    initial_tags: Mapping[str, str | int | float | bool] | None = None,
) -> Any:
    from pcbasm.pasting.paste_volume.dependencies import (
        dependency_versions,
        git_provenance,
        runtime_identity,
    )
    from pcbasm.pasting.paste_volume.experiment import (
        MLflowExperimentLogger,
        write_json_artifact,
    )
    from pcbasm.pasting.paste_volume.train import (
        sanitize_persisted_text,
        sanitize_persisted_value,
        summarize_persisted_git_diff,
    )

    tracking_uri = _require_tracking_server_uri(args.tracking_uri)
    provenance = git_provenance(Path.cwd())
    versions = dependency_versions()
    identity = runtime_identity()
    experiment_name = sanitize_persisted_text(str(args.experiment_name))
    run_name = (
        None if args.run_name is None else sanitize_persisted_text(str(args.run_name))
    )
    logger = MLflowExperimentLogger(
        tracking_uri=tracking_uri,
        experiment_name=experiment_name,
    )
    initial_run_tags = sanitize_persisted_value(
        {
            "git_branch": provenance.branch,
            "git_commit": provenance.commit,
            "git_dirty": provenance.dirty,
            **identity,
            **dict(initial_tags or {}),
        }
    )
    run_id = logger.start(
        run_kind=run_kind,
        run_name=run_name,
        tags=cast(Mapping[str, str | int | float | bool], initial_run_tags),
    )
    setattr(args, "_formal_run_id", run_id)
    formal_output = Path(args.output).expanduser().resolve()
    formal_attestation = formal_artifact_attestation_path(formal_output)
    try:
        if formal_attestation.exists():
            raise FileExistsError(
                f"formal success attestation already exists: {formal_attestation}"
            )
        arguments = _resolved_arguments(args)
        logger.log_params(
            {
                **{
                    f"arg.{key}": json.dumps(value, sort_keys=True)
                    for key, value in arguments.items()
                },
                **{f"version.{key}": value for key, value in versions.items()},
            }
        )
        with tempfile.TemporaryDirectory(
            prefix="pcbasm-paste-volume-operation-"
        ) as directory:
            temporary = Path(directory)
            arguments_path = write_json_artifact(
                temporary / "resolved-arguments.json", arguments
            )
            logger.log_artifact(arguments_path, artifact_path="operation")
            dependencies_path = write_json_artifact(
                temporary / "dependencies.json", versions
            )
            logger.log_artifact(dependencies_path, artifact_path="operation")
            persisted_diff = summarize_persisted_git_diff(provenance.diff)
            persisted_git = provenance.to_dict()
            persisted_git["diff"] = persisted_diff
            persisted_git["untracked_content"] = "[omitted at persistence boundary]"
            git_path = write_json_artifact(
                temporary / "git.json",
                cast(
                    Mapping[str, object],
                    sanitize_persisted_value(persisted_git),
                ),
            )
            logger.log_artifact(git_path, artifact_path="operation")
            git_diff_path = temporary / "git-diff.patch"
            git_diff_path.write_text(
                persisted_diff + "\n",
                encoding="utf-8",
            )
            logger.log_artifact(git_diff_path, artifact_path="operation")
            output = action()
            result_path = write_json_artifact(
                temporary / "result.json",
                {"result": _jsonable(output.value)},
            )
            logger.log_artifact(result_path, artifact_path="operation")
        logger.set_tags(
            cast(
                Mapping[str, str | int | float | bool],
                sanitize_persisted_value(dict(output.tags)),
            )
        )
        if output.metrics:
            logger.log_metrics(output.metrics, step=0)
        for artifact in output.artifacts:
            logger.log_artifact(artifact.resolve(), artifact_path="outputs")
        if output.failure_message is not None:
            raise RuntimeError(output.failure_message)
    except Exception as error:
        try:
            with tempfile.TemporaryDirectory(
                prefix="pcbasm-paste-volume-failure-"
            ) as directory:
                failure_path = write_json_artifact(
                    Path(directory) / "failure.json",
                    {
                        "error_type": type(error).__name__,
                        "message": sanitize_persisted_text(str(error)),
                    },
                )
                logger.log_artifact(failure_path, artifact_path="operation")
                for artifact in getattr(args, "_formal_failure_artifacts", ()):
                    logger.log_artifact(
                        Path(artifact).resolve(strict=True),
                        artifact_path="operation/failure-artifacts",
                    )
        except Exception:
            pass
        finally:
            try:
                logger.end(status="FAILED")
            except Exception:
                pass
        raise

    publish_formal_artifact(
        formal_output,
        tracking_uri=tracking_uri,
        run_kind=run_kind,
        logger=logger,
    )
    return output.value


def _dependency_group(error: ModuleNotFoundError) -> str | None:
    missing = (error.name or "").split(".", maxsplit=1)[0]
    return _DEPENDENCY_GROUP_BY_MODULE.get(missing)


def _invoke(action: Callable[[], Any]) -> int:
    try:
        result = action()
    except ModuleNotFoundError as error:
        dependency_group = _dependency_group(error)
        if dependency_group is None:
            raise
        print(
            f"missing optional ML dependency {error.name!r}; "
            f"run `uv sync --locked --group {dependency_group}` or `make setup-ml`",
            file=sys.stderr,
        )
        return 2
    except (OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if result is not None:
        _print_json(result)
    return 0


def _parse_source(value: str) -> tuple[str, Path]:
    source_id, separator, raw_path = value.partition("=")
    if not separator or not source_id or not raw_path:
        raise argparse.ArgumentTypeError("source must be SOURCE_ID=PATH")
    return source_id, Path(raw_path).expanduser().resolve()


def _dataset_inputs(paths: Sequence[str]) -> list[Any]:
    from pcbasm.pasting.paste_volume.data import DatasetInput

    return [
        DatasetInput(source_id=None, path=Path(path).expanduser().resolve())
        for path in paths
    ]


def _dataset_merge(args: argparse.Namespace) -> Any:
    from pcbasm.pasting.paste_volume.data import DatasetInput, merge_datasets

    source_ids = [source_id for source_id, _ in args.source]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("dataset source IDs must be unique")
    inputs = [
        DatasetInput(source_id=source_id, path=path) for source_id, path in args.source
    ]
    output = Path(args.output).expanduser().resolve()
    return merge_datasets(inputs, output, name=args.name or output.stem)


def _dataset_validate(args: argparse.Namespace) -> Any:
    from pcbasm.pasting.paste_volume.data import validate_datasets

    return validate_datasets(_dataset_inputs(args.datasets))


def _dataset_summarize(args: argparse.Namespace) -> Any:
    from pcbasm.pasting.paste_volume.data import (
        resolve_dataset_inputs,
        summarize_dataset,
    )

    roots = [Path(path).expanduser().resolve() for path in args.datasets]
    composite = resolve_dataset_inputs(roots=roots)
    return summarize_dataset(composite)


def _lineage_tags(lineage: Any) -> dict[str, str]:
    tags = {
        "source_run_id": str(lineage.source_run_id),
        "source_checkpoint_sha256": str(lineage.source_checkpoint_sha256),
        "dataset_fingerprint": str(lineage.dataset_fingerprint),
        "split_fingerprint": str(lineage.split_fingerprint),
    }
    if lineage.parent_run_id is not None:
        tags["parent_run_id"] = str(lineage.parent_run_id)
    if lineage.parent_checkpoint_id is not None:
        tags["parent_checkpoint_id"] = str(lineage.parent_checkpoint_id)
    return tags


def _write_report(path: Path, payload: Any) -> Path:
    if path.exists():
        raise FileExistsError(f"operation report already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
        json.dump(_jsonable(payload), stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return path


def _load_formal_training_weights(path: str, *, tracking_uri: str) -> Any:
    from pcbasm.pasting.paste_volume.training import load_formal_training_weights

    return load_formal_training_weights(
        Path(path).expanduser().resolve(strict=True),
        tracking_uri=tracking_uri,
    )


def _export(args: argparse.Namespace) -> Any:
    def action() -> _OperationOutput:
        from pcbasm.pasting.paste_volume.export import (
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


def _optimize(args: argparse.Namespace) -> Any:
    def action() -> _OperationOutput:
        from pcbasm.pasting.paste_volume.export import optimize_model

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


def _compile_parity(args: argparse.Namespace) -> Any:
    def action() -> _OperationOutput:
        from pcbasm.pasting.paste_volume.compile_parity import (
            CompileParityConfig,
            write_compile_parity_report,
        )
        from pcbasm.pasting.paste_volume.export import (
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


def _export_parity(args: argparse.Namespace) -> Any:
    def action() -> _OperationOutput:
        from pcbasm.pasting.paste_volume.export import (
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


def _infer(args: argparse.Namespace) -> Any:
    from pcbasm.pasting.paste_volume.inference import load_paste_volume_estimator

    model_package = Path(args.model_package).expanduser().resolve(strict=True)
    estimator = load_paste_volume_estimator(model_package)
    prediction = estimator.predict(
        _decode_rgb_image(args.pre_image),
        _decode_rgb_image(args.post_image),
        pixel_per_mm=args.pixel_per_mm,
    )
    return {"model": estimator.model_info, "prediction": prediction}


def _activate(args: argparse.Namespace) -> Any:
    from pcbasm.pasting.paste_volume.export import activate_model_package

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


def _rollback(args: argparse.Namespace) -> Any:
    from pcbasm.pasting.paste_volume.export import rollback_active_model

    return rollback_active_model(Path(args.pointer).expanduser().resolve(strict=True))


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


def _model_format_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model-format",
        choices=("onnx-fp32", "onnx-int8-qdq"),
        required=True,
    )


def _frozen_data_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data", nargs="+", required=True)
    parser.add_argument("--split-manifest", required=True)


def _frozen_test_receipt_path(selection: Path) -> Path:
    resolved = Path(selection).expanduser().resolve()
    return resolved.with_name(f"{resolved.name}.frozen-test-consumed.json")


def _frozen_test_data_binding(
    dataset_paths: Sequence[Path], split_manifest: Path
) -> dict[str, object]:
    from pcbasm.pasting.paste_volume.data import (
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


def _model_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--model-version", required=True)


def _candidate_evaluate(args: argparse.Namespace) -> Any:
    def action() -> _OperationOutput:
        from pcbasm.pasting.paste_volume.export import (
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


def _candidate_package(args: argparse.Namespace) -> Any:
    def action() -> _OperationOutput:
        from pcbasm.pasting.paste_volume.export import (
            load_model_candidate_validation,
            package_model_candidate_from_dataset,
        )

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


def _benchmark(args: argparse.Namespace) -> Any:
    def action() -> _OperationOutput:
        from pcbasm.pasting.paste_volume.export import (
            ArtifactLineage,
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


def _candidate_bind(args: argparse.Namespace) -> Any:
    def action() -> _OperationOutput:
        from pcbasm.pasting.paste_volume.compile_parity import (
            load_compile_parity_report,
        )
        from pcbasm.pasting.paste_volume.export import (
            build_model_candidate_validation,
            load_candidate_evaluation,
            load_export_parity_report,
            load_model_benchmark_result,
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


def _candidate_select(args: argparse.Namespace) -> Any:
    def action() -> _OperationOutput:
        from pcbasm.pasting.paste_volume.export import (
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


def _candidate_frozen_test(args: argparse.Namespace) -> Any:
    def action() -> _OperationOutput:
        from pcbasm.pasting.paste_volume.export import (
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
            run_id=str(args._formal_run_id),
        )
        setattr(args, "_formal_failure_artifacts", (receipt_path,))
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


def _candidate_finalize(args: argparse.Namespace) -> Any:
    def action() -> _OperationOutput:
        from pcbasm.pasting.paste_volume.export import (
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


def _promote(args: argparse.Namespace) -> Any:
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
        from pcbasm.pasting.paste_volume.export import (
            load_finalized_model_candidate,
            load_formal_cross_validation_evidence,
            promote_model_package_from_dataset,
        )

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


def _exercise_model(device_name: str) -> list[dict[str, Any]]:
    import torch

    from pcbasm.pasting.paste_volume.model import PasteVolumeResNet

    device = torch.device(device_name)
    model = PasteVolumeResNet().to(device)
    model.train()
    checks: list[dict[str, Any]] = []
    for height, width in ((64, 64), (1024, 256)):
        model.zero_grad(set_to_none=True)
        images = torch.randn((1, 6, height, width), device=device)
        valid_mask = torch.ones((1, 1, height, width), dtype=torch.bool, device=device)
        pixel_per_mm = torch.tensor([[30.0]], device=device)
        mean, log_variance = model(images, valid_mask, pixel_per_mm)
        if mean.shape != (1, 1) or log_variance.shape != (1, 1):
            raise RuntimeError("paste-volume model returned an unexpected output shape")
        (mean.sum() + log_variance.sum()).backward()
        if not any(parameter.grad is not None for parameter in model.parameters()):
            raise RuntimeError("paste-volume model backward produced no gradients")
        checks.append({"device": device_name, "height": height, "width": width})
    return checks


def _check_rgb_decode() -> dict[str, Any]:
    import torch
    from torchvision.io import ImageReadMode, decode_image, encode_png

    expected = torch.tensor(
        [[[11, 12]], [[21, 22]], [[31, 32]]],
        dtype=torch.uint8,
    )
    encoded = encode_png(expected)
    decoded = decode_image(encoded, mode=ImageReadMode.RGB)
    if not torch.equal(decoded, expected):
        raise RuntimeError("torchvision PNG decoding did not preserve RGB CHW values")
    return {"channel_order": "RGB", "shape": list(decoded.shape)}


def _compose_hydra(overrides: Sequence[str]) -> str:
    from hydra import compose, initialize_config_module
    from omegaconf import OmegaConf

    with initialize_config_module(
        config_module="pcbasm.pasting.paste_volume.conf",
        version_base=None,
    ):
        config = compose(config_name="train", overrides=list(overrides))
    return OmegaConf.to_yaml(config, resolve=True)


def _check_mlflow(tracking_uri: str) -> dict[str, str]:
    import mlflow
    from mlflow import MlflowClient

    tracking_uri = _require_tracking_server_uri(tracking_uri)
    mlflow.set_tracking_uri(tracking_uri)
    experiment = mlflow.set_experiment("pcbasm-paste-volume-smoke")
    with tempfile.TemporaryDirectory(prefix="pcbasm-ml-smoke-") as temporary_directory:
        artifact = Path(temporary_directory) / "smoke.txt"
        artifact.write_text("pcbasm paste-volume smoke\n", encoding="utf-8")
        with mlflow.start_run(experiment_id=experiment.experiment_id) as run:
            run_id = run.info.run_id
            mlflow.set_tag("run_kind", "smoke")
            mlflow.log_metric("smoke_metric", 1.0)
            mlflow.log_artifact(str(artifact), artifact_path="smoke")

        client = MlflowClient(tracking_uri=tracking_uri)
        stored = client.get_run(run_id)
        if stored.data.metrics.get("smoke_metric") != 1.0:
            raise RuntimeError("MLflow metric readback failed")
        destination = Path(temporary_directory) / "download"
        downloaded = Path(
            client.download_artifacts(run_id, "smoke/smoke.txt", str(destination))
        )
        if downloaded.read_text(encoding="utf-8") != artifact.read_text(
            encoding="utf-8"
        ):
            raise RuntimeError("MLflow artifact readback failed")
    return {"experiment_id": experiment.experiment_id, "run_id": run_id}


def _smoke(args: argparse.Namespace) -> Any:
    import hydra
    import mlflow
    import onnx
    import onnxruntime
    import onnxscript
    import optuna
    import torch
    import torchvision

    if args.require_cuda and not torch.cuda.is_available():
        raise RuntimeError("CUDA is required but torch.cuda.is_available() is false")

    versions = {
        "cuda": torch.version.cuda,
        "cudnn": torch.backends.cudnn.version(),
        "hydra": hydra.__version__,
        "mlflow": mlflow.__version__,
        "onnx": onnx.__version__,
        "onnxruntime": onnxruntime.__version__,
        "onnxscript": onnxscript.__version__,
        "optuna": optuna.__version__,
        "python": platform.python_version(),
        "torch": torch.__version__,
        "torchvision": torchvision.__version__,
    }
    devices = ["cpu"]
    if torch.cuda.is_available():
        devices.append("cuda")
    model_checks = [check for device in devices for check in _exercise_model(device)]
    hydra_overrides = args.hydra_override or [
        "data.manifest=/tmp/pcbasm-paste-volume-smoke.composite.json",
        "trainer.max_epochs=1",
        "trainer.compile_enabled=false",
        f"logger.tracking_uri={args.tracking_uri}",
    ]
    hydra_yaml = _compose_hydra(hydra_overrides)
    mlflow_result = _check_mlflow(args.tracking_uri)
    return {
        "hydra_config": hydra_yaml,
        "mlflow": mlflow_result,
        "model_checks": model_checks,
        "rgb_decode": _check_rgb_decode(),
        "versions": versions,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Operate the image-based paste-volume ML pipeline.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    smoke = commands.add_parser("smoke", help="verify the complete ML environment")
    smoke.add_argument(
        "--tracking-uri",
        default=os.environ.get("MLFLOW_TRACKING_URI", _DEFAULT_TRACKING_URI),
        help="MLflow tracking server URI",
    )
    smoke.add_argument(
        "--hydra-override",
        action="append",
        default=None,
        help="Hydra override used during config composition (repeatable)",
    )
    smoke.add_argument("--require-cuda", action="store_true")
    smoke.set_defaults(handler=_smoke)

    dataset = commands.add_parser("dataset", help="inspect or compose datasets")
    dataset_commands = dataset.add_subparsers(dest="dataset_command", required=True)

    merge = dataset_commands.add_parser("merge", help="write a flat composite manifest")
    merge.add_argument(
        "--source",
        action="append",
        required=True,
        type=_parse_source,
        metavar="SOURCE_ID=PATH",
    )
    merge.add_argument("--output", required=True)
    merge.add_argument("--name")
    merge.set_defaults(handler=_dataset_merge)

    validate = dataset_commands.add_parser(
        "validate", help="validate all dataset inputs"
    )
    validate.add_argument("datasets", nargs="+")
    validate.set_defaults(handler=_dataset_validate)

    summarize = dataset_commands.add_parser(
        "summarize", help="summarize resolved datasets"
    )
    summarize.add_argument("datasets", nargs="+")
    summarize.set_defaults(handler=_dataset_summarize)

    export = commands.add_parser("export", help="export strict weights to ONNX FP32")
    export.add_argument("weights")
    export.add_argument("--output", required=True, help="new export directory")
    export.add_argument("--opset-version", type=int, default=18)
    _tracking_arguments(export)
    export.set_defaults(handler=_export)

    optimize = commands.add_parser(
        "optimize", help="build optimized FP32 and static INT8 candidates"
    )
    optimize.add_argument("onnx_model")
    optimize.add_argument("--calibration-data", nargs="+", required=True)
    optimize.add_argument("--split-manifest", required=True)
    optimize.add_argument("--output", required=True, help="new candidate directory")
    optimize.add_argument("--calibration-sample-limit", type=int, default=256)
    optimize.add_argument("--seed", type=int, default=42)
    _tracking_arguments(optimize)
    optimize.set_defaults(handler=_optimize)

    compile_parity = commands.add_parser(
        "compile-parity",
        help=(
            "verify strict inductor/default torch.compile release parity "
            "from strict weights"
        ),
        description=(
            "Verify torch.compile release parity with the fixed backend=inductor "
            "and mode=default contract."
        ),
    )
    compile_parity.add_argument("weights")
    _frozen_data_arguments(compile_parity)
    compile_parity.add_argument(
        "--output",
        required=True,
        help=("new report JSON path, or a directory containing compile-parity.json"),
    )
    compile_parity.add_argument(
        "--device",
        choices=("auto", "cpu", "cuda"),
        default="auto",
        help="compile parity device selection (default: auto)",
    )
    _tracking_arguments(compile_parity)
    compile_parity.set_defaults(
        handler=_compile_parity,
        backend="inductor",
        mode="default",
    )

    export_parity = commands.add_parser(
        "export-parity",
        help="verify eager/FP32 ONNX parity on the persisted validation split",
    )
    export_parity.add_argument("weights")
    export_parity.add_argument("--fp32-model", required=True)
    _frozen_data_arguments(export_parity)
    export_parity.add_argument(
        "--output",
        required=True,
        help=("new report JSON path, or a directory containing export-parity.json"),
    )
    _tracking_arguments(export_parity)
    export_parity.set_defaults(handler=_export_parity)

    candidate = commands.add_parser(
        "candidate", help="evaluate, bind, select, and package model candidates"
    )
    candidate_commands = candidate.add_subparsers(
        dest="candidate_command", required=True
    )

    candidate_evaluate = candidate_commands.add_parser(
        "evaluate", help="evaluate one candidate on the frozen validation split"
    )
    candidate_evaluate.add_argument("model")
    candidate_evaluate.add_argument("--fp32-reference", required=True)
    _model_format_argument(candidate_evaluate)
    _frozen_data_arguments(candidate_evaluate)
    candidate_evaluate.add_argument("--output", required=True)
    _tracking_arguments(candidate_evaluate)
    candidate_evaluate.set_defaults(handler=_candidate_evaluate)

    candidate_package = candidate_commands.add_parser(
        "package", help="package a formally bound candidate with release evidence"
    )
    candidate_package.add_argument("candidate")
    candidate_package.add_argument("--output", required=True)
    _frozen_data_arguments(candidate_package)
    _model_identity_arguments(candidate_package)
    _tracking_arguments(candidate_package)
    candidate_package.set_defaults(handler=_candidate_package)

    candidate_bind = candidate_commands.add_parser(
        "bind", help="bind validation, benchmark, and the exact model artifact"
    )
    candidate_bind.add_argument("model")
    candidate_bind.add_argument("--evaluation", required=True)
    candidate_bind.add_argument("--benchmark", required=True)
    candidate_bind.add_argument("--compile-parity", required=True)
    candidate_bind.add_argument("--export-parity", required=True)
    candidate_bind.add_argument("--dependency-complexity-rank", type=int)
    candidate_bind.add_argument("--output", required=True)
    _tracking_arguments(candidate_bind)
    candidate_bind.set_defaults(handler=_candidate_bind)

    candidate_select = candidate_commands.add_parser(
        "select", help="freeze one candidate using validation and Pi benchmark only"
    )
    candidate_select.add_argument("candidates", nargs="+")
    candidate_select.add_argument("--output", required=True)
    _tracking_arguments(candidate_select)
    candidate_select.set_defaults(handler=_candidate_select)

    candidate_frozen_test = candidate_commands.add_parser(
        "frozen-test", help="evaluate the selected candidate once on frozen test"
    )
    candidate_frozen_test.add_argument("selection")
    candidate_frozen_test.add_argument("--fp32-reference", required=True)
    _frozen_data_arguments(candidate_frozen_test)
    candidate_frozen_test.add_argument(
        "--allow-external-split",
        action="store_true",
        help="allow an explicit external holdout for fine-tuned model release testing",
    )
    candidate_frozen_test.add_argument("--output", required=True)
    _tracking_arguments(candidate_frozen_test)
    candidate_frozen_test.set_defaults(handler=_candidate_frozen_test)

    candidate_finalize = candidate_commands.add_parser(
        "finalize", help="bind a selected candidate to a passing frozen-test report"
    )
    candidate_finalize.add_argument("selection")
    candidate_finalize.add_argument("--frozen-test", required=True)
    candidate_finalize.add_argument("--output", required=True)
    _tracking_arguments(candidate_finalize)
    candidate_finalize.set_defaults(handler=_candidate_finalize)

    benchmark = commands.add_parser(
        "benchmark", help="benchmark a candidate with production preprocessing"
    )
    benchmark.add_argument("model")
    _model_format_argument(benchmark)
    _frozen_data_arguments(benchmark)
    benchmark.add_argument("--output", required=True)
    benchmark.add_argument(
        "--power-condition",
        required=True,
        help="operator-declared benchmark power-supply condition",
    )
    benchmark.add_argument(
        "--cooling-condition",
        required=True,
        help="operator-declared benchmark cooling condition",
    )
    benchmark.add_argument(
        "--warmup-iterations",
        type=int,
        choices=(10,),
        default=10,
        help="fixed production benchmark warm-up count (must be 10)",
    )
    benchmark.add_argument(
        "--measured-iterations",
        type=int,
        choices=(100,),
        default=100,
        help="fixed production benchmark measurement count (must be 100)",
    )
    _tracking_arguments(benchmark)
    benchmark.set_defaults(handler=_benchmark)

    promote = commands.add_parser(
        "promote", help="build a production package from a finalized candidate"
    )
    promote.add_argument("finalized")
    promote.add_argument("--output", required=True)
    _frozen_data_arguments(promote)
    promote.add_argument(
        "--frozen-test-data",
        nargs="+",
        help=(
            "external frozen-test dataset root(s) or one composite manifest; "
            "requires --frozen-test-split-manifest"
        ),
    )
    promote.add_argument(
        "--frozen-test-split-manifest",
        help=(
            "persisted split for --frozen-test-data; when both options are omitted, "
            "--data/--split-manifest are reused"
        ),
    )
    promote.add_argument(
        "--cross-validation",
        action="append",
        required=True,
        metavar="REPORT",
        help=(
            "formally attested strict cross-validation report; repeat exactly "
            "once each for machine, paste_lot, and nozzle"
        ),
    )
    _model_identity_arguments(promote)
    _tracking_arguments(promote)
    promote.set_defaults(handler=_promote)

    infer = commands.add_parser("infer", help="run one promoted-package prediction")
    infer.add_argument("model_package")
    infer.add_argument("pre_image")
    infer.add_argument("post_image")
    infer.add_argument("--pixel-per-mm", type=float, required=True)
    infer.set_defaults(handler=_infer)

    activate = commands.add_parser(
        "activate", help="atomically switch an active-model pointer"
    )
    activate.add_argument("model_package")
    activate.add_argument("--pointer", required=True)
    activate.set_defaults(handler=_activate)

    rollback = commands.add_parser(
        "rollback", help="swap an active-model pointer back to its previous package"
    )
    rollback.add_argument("pointer")
    rollback.set_defaults(handler=_rollback)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return _invoke(lambda: args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
