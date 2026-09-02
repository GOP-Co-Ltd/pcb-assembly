"""Generic lifecycle for provenance-tracked command operations."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from .provenance import (
    dependency_versions,
    git_provenance,
    runtime_identity,
    sanitize_persisted_text,
    sanitize_persisted_uri,
    sanitize_persisted_value,
    summarize_persisted_git_diff,
)
from .runner import jsonable

Scalar = str | int | float | bool


class OperationLogger(Protocol):
    @property
    def run_id(self) -> str: ...

    def start(
        self,
        *,
        run_kind: str,
        run_name: str | None = None,
        tags: Mapping[str, Scalar] | None = None,
    ) -> str: ...

    def log_params(self, params: Mapping[str, Scalar]) -> None: ...

    def log_metrics(self, metrics: Mapping[str, float], *, step: int) -> None: ...

    def log_artifact(
        self,
        path: Path,
        *,
        artifact_path: str | None = None,
    ) -> None: ...

    def set_tags(self, tags: Mapping[str, Scalar]) -> None: ...

    def flush(self) -> None: ...

    def end(self, *, status: str = "FINISHED") -> None: ...


class OperationFinalizer(Protocol):
    def __call__(
        self,
        output: Path,
        *,
        tracking_uri: str,
        run_kind: str,
        logger: OperationLogger,
    ) -> object: ...


@dataclass(frozen=True)
class OperationOutput:
    value: Any
    artifacts: tuple[Path, ...] = ()
    metrics: Mapping[str, float] = field(default_factory=dict)
    tags: Mapping[str, Scalar] = field(default_factory=dict)
    failure_message: str | None = None


def require_tracking_server_uri(value: str) -> str:
    """Require the remote HTTP(S) tracking boundary used by formal runs."""

    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("MLflow tracking URI must be an HTTP(S) tracking server")
    return value


def resolved_arguments(args: argparse.Namespace) -> dict[str, Any]:
    """Return persistence-safe arguments without dispatch internals."""

    resolved: dict[str, Any] = {}
    for key, value in vars(args).items():
        if key == "handler" or key.startswith("_"):
            continue
        if key == "tracking_uri":
            resolved[key] = sanitize_persisted_uri(str(value))
        else:
            resolved[key] = jsonable(value)
    return cast(dict[str, Any], sanitize_persisted_value(resolved))


def _write_json_artifact(path: Path, payload: Mapping[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def run_tracked_operation(
    args: argparse.Namespace,
    *,
    run_kind: str,
    action: Callable[[], OperationOutput],
    logger: OperationLogger,
    finalizer: OperationFinalizer,
    output: Path,
    completion_marker: Path,
    initial_tags: Mapping[str, Scalar] | None = None,
    repository_root: Path | None = None,
) -> Any:
    """Run one operation, record provenance, and delegate success
    finalization."""

    tracking_uri = require_tracking_server_uri(str(args.tracking_uri))
    provenance = git_provenance(repository_root or Path.cwd())
    versions = dependency_versions()
    identity = runtime_identity()
    run_name_value = getattr(args, "run_name", None)
    run_name = (
        None if run_name_value is None else sanitize_persisted_text(str(run_name_value))
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
        tags=cast(Mapping[str, Scalar], initial_run_tags),
    )
    setattr(args, "_operation_run_id", run_id)
    resolved_output = output.expanduser().resolve()
    try:
        if completion_marker.exists():
            raise FileExistsError(
                f"operation completion marker already exists: {completion_marker}"
            )
        arguments = resolved_arguments(args)
        logger.log_params(
            {
                **{
                    f"arg.{key}": json.dumps(value, sort_keys=True)
                    for key, value in arguments.items()
                },
                **{f"version.{key}": value for key, value in versions.items()},
            }
        )
        with tempfile.TemporaryDirectory(prefix="ml-operation-") as directory:
            temporary = Path(directory)
            arguments_path = _write_json_artifact(
                temporary / "resolved-arguments.json", arguments
            )
            logger.log_artifact(arguments_path, artifact_path="operation")
            dependencies_path = _write_json_artifact(
                temporary / "dependencies.json", versions
            )
            logger.log_artifact(dependencies_path, artifact_path="operation")
            persisted_diff = summarize_persisted_git_diff(provenance.diff)
            persisted_git = provenance.to_dict()
            persisted_git["diff"] = persisted_diff
            persisted_git["untracked_content"] = "[omitted at persistence boundary]"
            git_path = _write_json_artifact(
                temporary / "git.json",
                cast(
                    Mapping[str, object],
                    sanitize_persisted_value(persisted_git),
                ),
            )
            logger.log_artifact(git_path, artifact_path="operation")
            git_diff_path = temporary / "git-diff.patch"
            git_diff_path.write_text(persisted_diff + "\n", encoding="utf-8")
            logger.log_artifact(git_diff_path, artifact_path="operation")
            operation_output = action()
            result_path = _write_json_artifact(
                temporary / "result.json",
                {"result": jsonable(operation_output.value)},
            )
            logger.log_artifact(result_path, artifact_path="operation")
        logger.set_tags(
            cast(
                Mapping[str, Scalar],
                sanitize_persisted_value(dict(operation_output.tags)),
            )
        )
        if operation_output.metrics:
            logger.log_metrics(operation_output.metrics, step=0)
        for artifact in operation_output.artifacts:
            logger.log_artifact(artifact.resolve(), artifact_path="outputs")
        if operation_output.failure_message is not None:
            raise RuntimeError(operation_output.failure_message)
    except Exception as error:
        try:
            with tempfile.TemporaryDirectory(
                prefix="ml-operation-failure-"
            ) as directory:
                failure_path = _write_json_artifact(
                    Path(directory) / "failure.json",
                    {
                        "error_type": type(error).__name__,
                        "message": sanitize_persisted_text(str(error)),
                    },
                )
                logger.log_artifact(failure_path, artifact_path="operation")
                for artifact in getattr(args, "_operation_failure_artifacts", ()):
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

    finalizer(
        resolved_output,
        tracking_uri=tracking_uri,
        run_kind=run_kind,
        logger=logger,
    )
    return operation_output.value
