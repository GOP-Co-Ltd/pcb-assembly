"""明示的な実験記録境界とMLflow adapter."""

from __future__ import annotations

import importlib
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit
from urllib.request import urlopen

Scalar = str | int | float | bool


@runtime_checkable
class ExperimentLogger(Protocol):
    """学習coreが利用する最小の実験記録契約."""

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


class NullExperimentLogger:
    """Pure training unit/integration用のin-memory logger.

    正式なHydra entrypointでは利用せず、呼び出し側が明示的に渡した場合だけ使う。
    """

    def __init__(self, run_id: str = "local-test-run") -> None:
        self._run_id = run_id
        self.params: dict[str, Scalar] = {}
        self.metrics: list[tuple[int, dict[str, float]]] = []
        self.tags: dict[str, Scalar] = {}
        self.artifacts: list[tuple[Path, str | None]] = []
        self.status: str | None = None

    @property
    def run_id(self) -> str:
        return self._run_id

    def start(
        self,
        *,
        run_kind: str,
        run_name: str | None = None,
        tags: Mapping[str, Scalar] | None = None,
    ) -> str:
        self.tags = {"run_kind": run_kind, **dict(tags or {})}
        if run_name is not None:
            self.tags["mlflow.runName"] = run_name
        return self._run_id

    def log_params(self, params: Mapping[str, Scalar]) -> None:
        self.params.update(params)

    def log_metrics(self, metrics: Mapping[str, float], *, step: int) -> None:
        self.metrics.append((step, dict(metrics)))

    def log_artifact(
        self,
        path: Path,
        *,
        artifact_path: str | None = None,
    ) -> None:
        if not path.is_file():
            raise FileNotFoundError(path)
        self.artifacts.append((path, artifact_path))

    def set_tags(self, tags: Mapping[str, Scalar]) -> None:
        self.tags.update(tags)

    def flush(self) -> None:
        return

    def end(self, *, status: str = "FINISHED") -> None:
        self.status = status


class MLflowExperimentLogger:
    """MLflow autologを使わない明示adapter."""

    def __init__(
        self,
        *,
        tracking_uri: str,
        experiment_name: str,
        resume_run_id: str | None = None,
        run_name: str | None = None,
        metric_retry_count: int = 3,
    ) -> None:
        if not tracking_uri:
            raise ValueError("tracking_uri is required")
        if not experiment_name:
            raise ValueError("experiment_name is required")
        if metric_retry_count < 1:
            raise ValueError("metric_retry_count must be positive")
        self._tracking_uri = tracking_uri
        self._experiment_name = experiment_name
        self._resume_run_id = resume_run_id
        self._run_name = run_name
        self._metric_retry_count = metric_retry_count
        self._mlflow: Any | None = None
        self._active_run: Any | None = None
        self._metric_queue: list[tuple[int, dict[str, float]]] = []

    @property
    def run_id(self) -> str:
        if self._active_run is None:
            raise RuntimeError("MLflow run has not started")
        return str(self._active_run.info.run_id)

    def _client_module(self) -> Any:
        if self._mlflow is None:
            self._mlflow = importlib.import_module("mlflow")
        return self._mlflow

    def verify_connection(self) -> None:
        """正式run開始前にtracking serverへの接続を確認する."""

        mlflow = self._client_module()
        mlflow.set_tracking_uri(self._tracking_uri)
        if urlsplit(self._tracking_uri).scheme in ("http", "https"):
            try:
                with urlopen(
                    self._tracking_uri.rstrip("/") + "/health", timeout=2.0
                ) as response:
                    if response.status != 200:
                        raise RuntimeError(
                            f"MLflow health check returned HTTP {response.status}"
                        )
            except OSError as error:
                raise RuntimeError("MLflow tracking server is unavailable") from error
        client = mlflow.tracking.MlflowClient()
        client.search_experiments(max_results=1)

    def start(
        self,
        *,
        run_kind: str,
        run_name: str | None = None,
        tags: Mapping[str, Scalar] | None = None,
    ) -> str:
        if self._active_run is not None:
            raise RuntimeError("MLflow run has already started")
        self.verify_connection()
        mlflow = self._client_module()
        mlflow.set_experiment(self._experiment_name)
        merged_tags = {"run_kind": run_kind, **dict(tags or {})}
        if self._resume_run_id is None:
            self._active_run = mlflow.start_run(
                run_name=run_name or self._run_name,
                tags={key: str(value) for key, value in merged_tags.items()},
            )
        else:
            self._active_run = mlflow.start_run(run_id=self._resume_run_id)
            self.set_tags(merged_tags)
        return self.run_id

    def _require_active(self) -> Any:
        if self._active_run is None:
            raise RuntimeError("MLflow run has not started")
        return self._client_module()

    def log_params(self, params: Mapping[str, Scalar]) -> None:
        mlflow = self._require_active()
        mlflow.log_params({key: value for key, value in params.items()})

    def log_metrics(self, metrics: Mapping[str, float], *, step: int) -> None:
        if any(not isinstance(value, (int, float)) for value in metrics.values()):
            raise TypeError("metric values must be numeric")
        self._metric_queue.append(
            (step, {key: float(value) for key, value in metrics.items()})
        )
        self.flush()

    def log_artifact(
        self,
        path: Path,
        *,
        artifact_path: str | None = None,
    ) -> None:
        if not path.is_file():
            raise FileNotFoundError(path)
        mlflow = self._require_active()
        mlflow.log_artifact(str(path), artifact_path=artifact_path)

    def set_tags(self, tags: Mapping[str, Scalar]) -> None:
        mlflow = self._require_active()
        mlflow.set_tags({key: value for key, value in tags.items()})

    def flush(self) -> None:
        mlflow = self._require_active()
        queued = list(self._metric_queue)
        for index, (step, metrics) in enumerate(queued):
            sent = False
            last_error: Exception | None = None
            for _ in range(self._metric_retry_count):
                try:
                    mlflow.log_metrics(metrics, step=step, synchronous=True)
                    sent = True
                    break
                except Exception as error:  # MLflow exposes backend-specific failures
                    last_error = error
            if not sent:
                self._metric_queue = queued[index:]
                raise RuntimeError("failed to flush MLflow metrics") from last_error
        self._metric_queue = []

    def end(self, *, status: str = "FINISHED") -> None:
        mlflow = self._require_active()
        try:
            self.flush()
        except Exception:
            mlflow.end_run(status="FAILED")
            self._active_run = None
            raise
        mlflow.end_run(status=status)
        self._active_run = None


def write_json_artifact(path: Path, payload: Mapping[str, object]) -> Path:
    """決定的なJSON artifactを作る."""

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
        if temporary.exists():
            temporary.unlink()
    return path


__all__ = [
    "ExperimentLogger",
    "MLflowExperimentLogger",
    "NullExperimentLogger",
    "Scalar",
    "write_json_artifact",
]
