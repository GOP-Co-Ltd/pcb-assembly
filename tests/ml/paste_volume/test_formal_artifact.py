from __future__ import annotations

import json
from pathlib import Path
from typing import override

import pytest
from mlflow import MlflowClient

from ml.artifacts.formal import formal_artifact_attestation_path
from ml.paste_volume.formal_artifact import (
    FormalArtifactAttestation,
    publish_formal_artifact,
    verify_formal_artifact,
)
from ml.training.experiment import MLflowExperimentLogger, NullExperimentLogger


class _RecordingLogger(NullExperimentLogger):
    def __init__(self, run_id: str = "recording-run") -> None:
        super().__init__(run_id)
        self.remote_attestation: dict[str, object] | None = None

    @override
    def log_artifact(self, path: Path, *, artifact_path: str | None = None) -> None:
        super().log_artifact(path, artifact_path=artifact_path)
        if path.name == "formal-success.json":
            value = json.loads(path.read_text(encoding="utf-8"))
            assert isinstance(value, dict)
            self.remote_attestation = value


class _FailingArtifactLogger(_RecordingLogger):
    @override
    def log_artifact(self, path: Path, *, artifact_path: str | None = None) -> None:
        del path, artifact_path
        raise RuntimeError("remote artifact write failed")


class _TamperingLogger(_RecordingLogger):
    def __init__(self, output: Path) -> None:
        super().__init__("tampering-run")
        self._output = output

    @override
    def end(self, *, status: str = "FINISHED") -> None:
        super().end(status=status)
        if status == "FINISHED":
            self._output.write_text("changed-after-finish\n", encoding="utf-8")


class TestFormalArtifactAttestation:
    def test_round_trips_only_the_strict_schema(self, tmp_path: Path):
        attestation = FormalArtifactAttestation(
            run_id="run-1",
            run_kind="evaluate",
            tracking_uri_sha256="sha256:" + "1" * 64,
            output_path=(tmp_path / "report.json").resolve(),
            output_kind="file",
            output_fingerprint="sha256:" + "2" * 64,
        )

        assert FormalArtifactAttestation.from_dict(attestation.to_dict()) == attestation
        invalid = {**attestation.to_dict(), "unexpected": True}
        with pytest.raises(ValueError, match="key set"):
            FormalArtifactAttestation.from_dict(invalid)

    def test_rejects_non_sha256_fingerprints(self, tmp_path: Path):
        with pytest.raises(ValueError, match="output_fingerprint"):
            FormalArtifactAttestation(
                run_id="run-1",
                run_kind="evaluate",
                tracking_uri_sha256="sha256:" + "1" * 64,
                output_path=(tmp_path / "report.json").resolve(),
                output_kind="file",
                output_fingerprint="not-a-fingerprint",
            )


class TestPublishFormalArtifact:
    def test_publishes_remote_then_finished_then_create_only_local(
        self, tmp_path: Path
    ):
        output = tmp_path / "report.json"
        output.write_text('{"success": true}\n', encoding="utf-8")
        tracking_uri = (
            "https://user:password@example.invalid/mlflow?token=secret#fragment"
        )
        logger = _RecordingLogger()
        logger.start(run_kind="evaluate")

        attestation = publish_formal_artifact(
            output,
            tracking_uri=tracking_uri,
            run_kind="evaluate",
            logger=logger,
        )

        local = formal_artifact_attestation_path(output)
        persisted = local.read_text(encoding="utf-8")
        assert logger.status == "FINISHED"
        assert logger.remote_attestation == attestation.to_dict()
        assert json.loads(persisted) == attestation.to_dict()
        assert attestation.output_path == output.resolve()
        assert "password" not in persisted
        assert "token=secret" not in persisted

    def test_existing_sibling_fails_closed_without_overwrite(self, tmp_path: Path):
        output = tmp_path / "report.json"
        output.write_text("original\n", encoding="utf-8")
        local = formal_artifact_attestation_path(output)
        local.write_text("do-not-overwrite\n", encoding="utf-8")
        logger = _RecordingLogger()
        logger.start(run_kind="evaluate")

        with pytest.raises(FileExistsError, match="already exists"):
            publish_formal_artifact(
                output,
                tracking_uri="https://mlflow.invalid",
                run_kind="evaluate",
                logger=logger,
            )

        assert logger.status == "FAILED"
        assert local.read_text(encoding="utf-8") == "do-not-overwrite\n"

    def test_logging_failure_closes_failed_without_local_attestation(
        self, tmp_path: Path
    ):
        output = tmp_path / "report.json"
        output.write_text("original\n", encoding="utf-8")
        logger = _FailingArtifactLogger()
        logger.start(run_kind="evaluate")

        with pytest.raises(RuntimeError, match="remote artifact write failed"):
            publish_formal_artifact(
                output,
                tracking_uri="https://mlflow.invalid",
                run_kind="evaluate",
                logger=logger,
            )

        assert logger.status == "FAILED"
        assert not formal_artifact_attestation_path(output).exists()

    def test_output_change_after_finished_never_publishes_local_attestation(
        self, tmp_path: Path
    ):
        output = tmp_path / "report.json"
        output.write_text("original\n", encoding="utf-8")
        logger = _TamperingLogger(output)
        logger.start(run_kind="evaluate")

        with pytest.raises(RuntimeError, match="changed"):
            publish_formal_artifact(
                output,
                tracking_uri="https://mlflow.invalid",
                run_kind="evaluate",
                logger=logger,
            )

        assert logger.status == "FINISHED"
        assert not formal_artifact_attestation_path(output).exists()

    @pytest.mark.parametrize("entry", ("empty", "symlink"))
    def test_rejects_unsafe_directory_outputs(self, tmp_path: Path, entry: str):
        output = tmp_path / "output"
        output.mkdir()
        if entry == "symlink":
            target = tmp_path / "target.json"
            target.write_text("target\n", encoding="utf-8")
            (output / "linked.json").symlink_to(target)
        logger = _RecordingLogger()
        logger.start(run_kind="evaluate")

        with pytest.raises(ValueError, match="empty|symlink"):
            publish_formal_artifact(
                output,
                tracking_uri="https://mlflow.invalid",
                run_kind="evaluate",
                logger=logger,
            )

        assert logger.status == "FAILED"


class TestVerifyFormalArtifact:
    def test_verifies_real_mlflow_run_tags_and_remote_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        output = tmp_path / "report.json"
        output.write_text('{"success": true}\n', encoding="utf-8")
        monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
        tracking_uri = (tmp_path / "mlruns").resolve().as_uri()
        logger = MLflowExperimentLogger(
            tracking_uri=tracking_uri,
            experiment_name="formal-artifact-test",
        )
        logger.start(run_kind="cross-validation-summary")
        produced = publish_formal_artifact(
            output,
            tracking_uri=tracking_uri,
            run_kind="cross-validation-summary",
            logger=logger,
        )

        verified = verify_formal_artifact(
            output,
            tracking_uri=tracking_uri,
            expected_run_kind="cross-validation-summary",
        )

        assert verified == produced

        output.write_text('{"success": false}\n', encoding="utf-8")
        with pytest.raises(ValueError, match="does not match artifact"):
            verify_formal_artifact(output, tracking_uri=tracking_uri)

        output.write_text('{"success": true}\n', encoding="utf-8")
        replacement = tmp_path / "remote-replacement" / "formal-success.json"
        replacement.parent.mkdir()
        replacement.write_text(
            json.dumps({**produced.to_dict(), "run_id": "different-run"}),
            encoding="utf-8",
        )
        client = MlflowClient(tracking_uri=tracking_uri)
        client.log_artifact(
            produced.run_id, str(replacement), artifact_path="operation"
        )
        with pytest.raises(ValueError, match="local and remote"):
            verify_formal_artifact(output, tracking_uri=tracking_uri)

        client.set_terminated(produced.run_id, status="FAILED")
        with pytest.raises(ValueError, match="not FINISHED"):
            verify_formal_artifact(output, tracking_uri=tracking_uri)

    def test_rejects_wrong_expected_run_kind_before_remote_access(self, tmp_path: Path):
        output = tmp_path / "report.json"
        output.write_text("report\n", encoding="utf-8")
        logger = _RecordingLogger()
        logger.start(run_kind="evaluate")
        publish_formal_artifact(
            output,
            tracking_uri="https://mlflow.invalid",
            run_kind="evaluate",
            logger=logger,
        )

        with pytest.raises(ValueError, match="run_kind"):
            verify_formal_artifact(
                output,
                tracking_uri="https://mlflow.invalid",
                expected_run_kind="benchmark",
            )
