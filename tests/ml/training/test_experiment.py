"""Explicit MLflow experiment adapter integration tests."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mlflow import MlflowClient

from ml.training.experiment import MLflowExperimentLogger, NullExperimentLogger


def _free_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


class _MLflowServer:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._port = _free_port()
        self.uri = f"http://127.0.0.1:{self._port}"
        self._process: subprocess.Popen[str] | None = None

    def start(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self._process = subprocess.Popen(
            (
                sys.executable,
                "-m",
                "mlflow",
                "server",
                "--backend-store-uri",
                f"sqlite:///{self._root / 'mlflow.db'}",
                "--default-artifact-root",
                (self._root / "artifacts").as_uri(),
                "--host",
                "127.0.0.1",
                "--port",
                str(self._port),
                "--workers",
                "1",
            ),
            cwd=self._root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        client = MlflowClient(tracking_uri=self.uri)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError("MLflow server exited during startup")
            try:
                client.search_experiments(max_results=1)
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("MLflow server did not become ready")

    def stop(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        self._process.wait(timeout=10)
        self._process = None


class TestNullExperimentLogger:
    def test_records_explicit_events(self, tmp_path: Path):
        artifact = tmp_path / "report.json"
        artifact.write_text("{}", encoding="utf-8")
        logger = NullExperimentLogger("run-1")

        assert logger.start(run_kind="base-train", tags={"dataset": "abc"}) == "run-1"
        logger.log_params({"learning_rate": 1e-3})
        logger.log_metrics({"validation/nll": 0.5}, step=2)
        logger.log_artifact(artifact, artifact_path="reports")
        logger.end()

        assert logger.tags["run_kind"] == "base-train"
        assert logger.params["learning_rate"] == 1e-3
        assert logger.metrics == [(2, {"validation/nll": 0.5})]
        assert logger.artifacts == [(artifact, "reports")]
        assert logger.status == "FINISHED"


class TestMLflowExperimentLogger:
    def test_sqlite_logs_and_resumes_same_run(self, tmp_path: Path):
        tracking_uri = f"sqlite:///{tmp_path / 'tracking.db'}"
        artifacts = tmp_path / "artifacts"
        client = MlflowClient(tracking_uri=tracking_uri)
        client.create_experiment("paste-volume", artifact_location=artifacts.as_uri())
        artifact = tmp_path / "report.json"
        artifact.write_text('{"ok": true}', encoding="utf-8")
        logger = MLflowExperimentLogger(
            tracking_uri=tracking_uri,
            experiment_name="paste-volume",
        )

        run_id = logger.start(run_kind="base-train")
        logger.log_params({"seed": 42})
        logger.log_metrics({"validation/nll": 0.25}, step=3)
        logger.log_artifact(artifact, artifact_path="reports")
        logger.end()
        resumed = MLflowExperimentLogger(
            tracking_uri=tracking_uri,
            experiment_name="paste-volume",
            resume_run_id=run_id,
        )
        assert resumed.start(run_kind="base-train") == run_id
        resumed.log_metrics({"validation/mae_ul": 0.1}, step=4)
        resumed.end()

        run = client.get_run(run_id)
        downloaded = Path(
            client.download_artifacts(
                run_id, "reports/report.json", str(tmp_path / "download")
            )
        )
        assert run.info.status == "FINISHED"
        assert run.data.params["seed"] == "42"
        assert run.data.metrics["validation/nll"] == pytest.approx(0.25)
        assert run.data.metrics["validation/mae_ul"] == pytest.approx(0.1)
        assert downloaded.read_text(encoding="utf-8") == '{"ok": true}'

    def test_real_http_server_round_trip(self, tmp_path: Path):
        server = _MLflowServer(tmp_path / "server")
        server.start()
        try:
            artifact = tmp_path / "artifact.txt"
            artifact.write_text("round-trip", encoding="utf-8")
            logger = MLflowExperimentLogger(
                tracking_uri=server.uri,
                experiment_name="paste-volume-http",
            )

            run_id = logger.start(run_kind="base-train")
            logger.log_metrics({"validation/nll": 1.5}, step=0)
            logger.log_artifact(artifact, artifact_path="smoke")
            logger.end()

            client = MlflowClient(tracking_uri=server.uri)
            run = client.get_run(run_id)
            downloaded = Path(
                client.download_artifacts(
                    run_id, "smoke/artifact.txt", str(tmp_path / "http-download")
                )
            )
            assert run.info.status == "FINISHED"
            assert run.data.metrics["validation/nll"] == pytest.approx(1.5)
            assert downloaded.read_text(encoding="utf-8") == "round-trip"
        finally:
            server.stop()

    def test_connection_failure_prevents_run_start(self):
        port = _free_port()
        logger = MLflowExperimentLogger(
            tracking_uri=f"http://127.0.0.1:{port}",
            experiment_name="unavailable",
        )

        with pytest.raises(Exception):
            logger.start(run_kind="base-train")

        with pytest.raises(RuntimeError, match="has not started"):
            _ = logger.run_id
