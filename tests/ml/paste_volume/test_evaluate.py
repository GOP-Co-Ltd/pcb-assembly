from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import pytest
import torch
from mlflow import MlflowClient

from ml.artifacts.formal import formal_artifact_attestation_path
from ml.data.image import AugmentationConfig, ImageConstraints
from ml.paste_volume.data import (
    build_sample_index,
    create_session_split,
    resolve_dataset_inputs,
    save_split_manifest,
)
from ml.paste_volume.evaluate import (
    EVALUATION_REPORT_KIND,
    EvaluateConfig,
    evaluate,
    evaluate_config_from_mapping,
)
from ml.paste_volume.formal_artifact import (
    publish_formal_artifact,
    verify_formal_artifact,
)
from ml.paste_volume.train import DataConfig
from ml.training.experiment import MLflowExperimentLogger, NullExperimentLogger
from tests.ml.paste_volume.support_data import write_synthetic_session
from tests.ml.paste_volume.support_runtime import (
    make_test_lineage,
    write_test_weights,
)

_REPOSITORY_ROOT = Path(__file__).parents[3]
_PROTOCOL_FINGERPRINT = "sha256:" + "f" * 64


@dataclass(frozen=True)
class _EvaluationFixture:
    data: DataConfig
    dataset_fingerprint: str
    split_fingerprint: str


def _write_evaluation_fixture(
    directory: Path, *, identity: str, content_seed: int
) -> _EvaluationFixture:
    root = directory / f"dataset-{identity}"
    for index in range(3):
        write_synthetic_session(
            root,
            f"{identity}-session-{index}",
            machine_id=f"machine-{identity}",
            pad_count=1,
            width=40,
            height=40,
            content_seed=content_seed + index,
        )
    composite = resolve_dataset_inputs(roots=(root,))
    samples = build_sample_index(composite)
    split = create_session_split(
        samples,
        composite.composite_fingerprint,
        seed=17,
        mode="base",
    )
    split_path = directory / f"split-{identity}.json"
    save_split_manifest(split, split_path)
    return _EvaluationFixture(
        data=DataConfig(
            roots=(root,),
            split_manifest=split_path,
            constraints=ImageConstraints(
                min_size=32,
                max_size=1024,
                max_pixels=262_144,
                stride=32,
            ),
            augmentation=AugmentationConfig(enabled=False),
            max_batch_pixels=262_144,
            max_batch_size=4,
        ),
        dataset_fingerprint=composite.composite_fingerprint,
        split_fingerprint=split.split_fingerprint,
    )


def _write_weights(path: Path, fixture: _EvaluationFixture) -> Path:
    return write_test_weights(
        path,
        make_test_lineage(
            dataset_fingerprint=fixture.dataset_fingerprint,
            split_fingerprint=fixture.split_fingerprint,
            training_protocol_fingerprint=_PROTOCOL_FINGERPRINT,
        ),
    )


def _config(
    fixture: _EvaluationFixture,
    *,
    weights: Path,
    output: Path,
    split: Literal["validation", "test"] = "validation",
    allow_frozen_test: bool = False,
    allow_external_split: bool = False,
    compile_enabled: bool = False,
) -> EvaluateConfig:
    return EvaluateConfig(
        data=fixture.data,
        weights=weights,
        output_directory=output,
        split=split,
        allow_frozen_test=allow_frozen_test,
        allow_external_split=allow_external_split,
        device="cpu",
        compile_enabled=compile_enabled,
        compile_backend="eager" if compile_enabled else "inductor",
        compile_mode="default",
        repository_root=_REPOSITORY_ROOT,
    )


def _formalize_weights(
    weights: Path,
    fixture: _EvaluationFixture,
    *,
    tracking_uri: str,
) -> str:
    MlflowClient(tracking_uri=tracking_uri).create_experiment(
        "paste-volume-source-test",
        artifact_location=(weights.parent / "source-artifacts").resolve().as_uri(),
    )
    logger = MLflowExperimentLogger(
        tracking_uri=tracking_uri,
        experiment_name="paste-volume-source-test",
    )
    source_run_id = logger.start(
        run_kind="base-train",
        tags={
            "dataset_fingerprint": fixture.dataset_fingerprint,
            "split_fingerprint": fixture.split_fingerprint,
            "training_protocol_fingerprint": _PROTOCOL_FINGERPRINT,
        },
    )
    payload = torch.load(weights, map_location="cpu", weights_only=True)
    payload["source_run_id"] = source_run_id
    torch.save(payload, weights)
    logger.log_artifact(weights, artifact_path="checkpoints")
    publish_formal_artifact(
        weights,
        tracking_uri=tracking_uri,
        run_kind="base-train",
        logger=logger,
    )
    return source_run_id


class TestEvaluateConfig:
    @pytest.mark.parametrize(
        ("split", "allow_frozen_test"),
        (("test", False), ("validation", True)),
    )
    def test_test_split_and_diagnostic_acknowledgement_are_atomic(
        self,
        tmp_path: Path,
        split: Literal["validation", "test"],
        allow_frozen_test: bool,
    ):
        fixture = _write_evaluation_fixture(tmp_path, identity="guard", content_seed=10)

        with pytest.raises(ValueError, match="together"):
            _config(
                fixture,
                weights=tmp_path / "weights.pt",
                output=tmp_path / "evaluation",
                split=split,
                allow_frozen_test=allow_frozen_test,
            )

    def test_strict_mapping_keeps_compile_enabled_by_default(self, tmp_path: Path):
        mapping = {
            "data": {
                "manifest": None,
                "roots": ["dataset"],
                "split_manifest": "split.json",
                "split_seed": 42,
                "train_ratio": 0.7,
                "validation_ratio": 0.15,
                "test_ratio": 0.15,
                "constraints": {
                    "min_size": 32,
                    "max_size": 1024,
                    "max_pixels": 262_144,
                    "stride": 32,
                    "normalization_epsilon": 1e-5,
                },
                "augmentation": {
                    "enabled": False,
                    "min_scale": 0.8,
                    "max_scale": 1.2,
                },
                "max_batch_pixels": 262_144,
                "max_batch_size": 4,
            },
            "logger": {
                "tracking_uri": "https://mlflow.example.invalid",
                "experiment_name": "paste-volume",
                "run_name": None,
                "metric_retry_count": 3,
            },
            "weights": "weights.pt",
            "output_directory": "evaluation",
            "split": "validation",
            "allow_frozen_test": False,
            "allow_external_split": False,
            "device": "cpu",
            "compile_backend": "inductor",
            "compile_mode": "default",
            "repository_root": str(_REPOSITORY_ROOT),
        }

        config, _ = evaluate_config_from_mapping(mapping, base_directory=tmp_path)

        assert config.compile_enabled
        assert config.compile_backend == "inductor"
        assert config.compile_mode == "default"
        with pytest.raises(TypeError, match="compile_enabled"):
            evaluate_config_from_mapping(
                {**mapping, "compile_enabled": "false"},
                base_directory=tmp_path,
            )
        with pytest.raises(ValueError, match="unknown evaluate"):
            evaluate_config_from_mapping(
                {**mapping, "unexpected": True}, base_directory=tmp_path
            )


class TestEvaluationModes:
    def test_validation_records_complete_provenance_and_compile_preflight(
        self, tmp_path: Path
    ):
        fixture = _write_evaluation_fixture(
            tmp_path, identity="validation", content_seed=100
        )
        weights = _write_weights(tmp_path / "weights.pt", fixture)
        secret_uri = "https://user:password@example.invalid/mlflow?token=secret#part"
        config = replace(
            _config(
                fixture,
                weights=weights,
                output=tmp_path / "evaluation",
                compile_enabled=True,
            ),
            hydra_resolved_yaml=f"logger:\n  tracking_uri: {secret_uri}\n",
            hydra_overrides=(f"logger.tracking_uri={secret_uri}",),
        )
        logger = NullExperimentLogger("evaluate-validation")

        result = evaluate(config, logger)

        report = json.loads(result.report_path.read_text(encoding="utf-8"))
        assert report["kind"] == EVALUATION_REPORT_KIND
        assert report["diagnostic_only"] is False
        assert report["release_evidence"] is True
        assert report["external_evaluation_split"] is False
        assert result.release_evidence
        assert not result.diagnostic_only
        assert logger.status == "FINISHED"
        expected_artifacts = {
            "composite.json",
            "sample-index.json",
            "split.json",
            "dataset-validation.json",
            "config.json",
            "resolved-config.yaml",
            "hydra-overrides.json",
            "dependencies.json",
            "git.diff",
            "git.json",
            "model-summary.json",
            "parameters.json",
            "compile-preflight.json",
            "evaluation-validation.json",
            "diagnostics-validation.json",
        }
        assert expected_artifacts <= {
            path.name for path in config.output_directory.iterdir()
        }
        compile_report = json.loads(
            (config.output_directory / "compile-preflight.json").read_text(
                encoding="utf-8"
            )
        )
        assert compile_report["enabled"] is True
        assert compile_report["graph_break_count"] == 0
        persisted = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in config.output_directory.iterdir()
            if path.is_file()
        )
        persisted += json.dumps(logger.tags) + json.dumps(logger.params)
        assert "password" not in persisted
        assert "token=secret" not in persisted

    def test_external_validation_requires_and_records_explicit_opt_in(
        self, tmp_path: Path
    ):
        training = _write_evaluation_fixture(
            tmp_path, identity="training", content_seed=200
        )
        external = _write_evaluation_fixture(
            tmp_path, identity="external", content_seed=300
        )
        weights = _write_weights(tmp_path / "weights.pt", training)

        rejected_logger = NullExperimentLogger("external-rejected")
        with pytest.raises(ValueError, match="allow_external_split"):
            evaluate(
                _config(
                    external,
                    weights=weights,
                    output=tmp_path / "rejected",
                ),
                rejected_logger,
            )
        assert rejected_logger.status == "FAILED"

        logger = NullExperimentLogger("external-accepted")
        result = evaluate(
            _config(
                external,
                weights=weights,
                output=tmp_path / "accepted",
                allow_external_split=True,
            ),
            logger,
        )
        report = json.loads(result.report_path.read_text(encoding="utf-8"))
        assert result.external_evaluation_split
        assert report["training_lineage"]["dataset_fingerprint"] == (
            training.dataset_fingerprint
        )
        assert report["evaluation_lineage"]["dataset_fingerprint"] == (
            external.dataset_fingerprint
        )
        assert logger.tags["external_evaluation_split"] == "true"

    def test_external_flag_without_external_lineage_is_rejected(self, tmp_path: Path):
        fixture = _write_evaluation_fixture(tmp_path, identity="same", content_seed=400)
        weights = _write_weights(tmp_path / "weights.pt", fixture)
        logger = NullExperimentLogger("external-flag-alone")

        with pytest.raises(ValueError, match="requires a different persisted"):
            evaluate(
                _config(
                    fixture,
                    weights=weights,
                    output=tmp_path / "evaluation",
                    allow_external_split=True,
                ),
                logger,
            )

        assert logger.status == "FAILED"

    def test_generic_test_evaluation_is_explicitly_diagnostic_only(
        self, tmp_path: Path
    ):
        fixture = _write_evaluation_fixture(
            tmp_path, identity="diagnostic", content_seed=500
        )
        weights = _write_weights(tmp_path / "weights.pt", fixture)
        logger = NullExperimentLogger("evaluate-diagnostic-test")

        result = evaluate(
            _config(
                fixture,
                weights=weights,
                output=tmp_path / "evaluation",
                split="test",
                allow_frozen_test=True,
            ),
            logger,
        )

        report = json.loads(result.report_path.read_text(encoding="utf-8"))
        assert result.diagnostic_only
        assert not result.release_evidence
        assert report["diagnostic_only"] is True
        assert report["release_evidence"] is False
        assert logger.tags["diagnostic_only"] == "true"
        assert logger.tags["release_evidence"] == "false"


class TestFormalMLflowEvaluation:
    def test_formal_weights_and_finished_evaluation_are_bound_end_to_end(
        self, tmp_path: Path
    ):
        fixture = _write_evaluation_fixture(
            tmp_path, identity="formal", content_seed=600
        )
        weights = _write_weights(tmp_path / "weights.pt", fixture)
        tracking_uri = f"sqlite:///{tmp_path / 'tracking.db'}"
        source_run_id = _formalize_weights(weights, fixture, tracking_uri=tracking_uri)
        logger = MLflowExperimentLogger(
            tracking_uri=tracking_uri,
            experiment_name="paste-volume-evaluate-test",
        )
        MlflowClient(tracking_uri=tracking_uri).create_experiment(
            "paste-volume-evaluate-test",
            artifact_location=(tmp_path / "evaluate-artifacts").resolve().as_uri(),
        )

        result = evaluate(
            _config(
                fixture,
                weights=weights,
                output=tmp_path / "evaluation",
            ),
            logger,
            formal_tracking_uri=tracking_uri,
        )

        attestation = verify_formal_artifact(
            result.report_path,
            tracking_uri=tracking_uri,
            expected_run_kind="evaluate",
        )
        assert attestation.run_id == result.run_id
        assert formal_artifact_attestation_path(result.report_path).is_file()
        report = json.loads(result.report_path.read_text(encoding="utf-8"))
        assert report["formal_training_weights_verified"] is True
        assert report["training_lineage"]["run_id"] == source_run_id
        run = MlflowClient(tracking_uri=tracking_uri).get_run(result.run_id)
        assert run.info.status == "FINISHED"
        assert run.data.tags["release_evidence"] == "true"

    def test_post_start_weight_failure_marks_real_run_failed_without_receipt(
        self, tmp_path: Path
    ):
        fixture = _write_evaluation_fixture(
            tmp_path, identity="failure", content_seed=700
        )
        tracking_uri = f"sqlite:///{tmp_path / 'tracking.db'}"
        experiment_name = "paste-volume-evaluate-failure-test"
        MlflowClient(tracking_uri=tracking_uri).create_experiment(
            experiment_name,
            artifact_location=(tmp_path / "failure-artifacts").resolve().as_uri(),
        )
        logger = MLflowExperimentLogger(
            tracking_uri=tracking_uri,
            experiment_name=experiment_name,
        )
        output = tmp_path / "evaluation"

        with pytest.raises(ValueError, match="do not exist"):
            evaluate(
                _config(
                    fixture,
                    weights=tmp_path / "missing.pt",
                    output=output,
                ),
                logger,
                formal_tracking_uri=tracking_uri,
            )

        client = MlflowClient(tracking_uri=tracking_uri)
        experiment = client.get_experiment_by_name(experiment_name)
        assert experiment is not None
        runs = client.search_runs([experiment.experiment_id])
        assert len(runs) == 1
        assert runs[0].info.status == "FAILED"
        assert not formal_artifact_attestation_path(
            output / "evaluation-validation.json"
        ).exists()
