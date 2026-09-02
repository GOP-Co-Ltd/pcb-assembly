from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast, override

import pytest
from hydra import compose, initialize_config_module
from omegaconf import OmegaConf

from pcbasm.pasting.paste_volume.cross_validate import (
    cross_validation_config_from_mapping,
    run_formal_cross_validation,
)
from pcbasm.pasting.paste_volume.cross_validation import (
    CrossValidationConfig,
    CrossValidationLoggerFactory,
    CrossValidationPhase,
)
from pcbasm.pasting.paste_volume.data import AugmentationConfig, ImageConstraints
from pcbasm.pasting.paste_volume.experiment import NullExperimentLogger, Scalar
from pcbasm.pasting.paste_volume.formal_artifact import (
    formal_artifact_attestation_path,
)
from pcbasm.pasting.paste_volume.model import PasteVolumeModelConfig
from pcbasm.pasting.paste_volume.reporting import CrossGroupFold
from pcbasm.pasting.paste_volume.train import DataConfig, LoggerConfig, TrainConfig
from pcbasm.pasting.paste_volume.training import CheckpointConfig, TrainerConfig
from tests.pcbasm.pasting.paste_volume.support_data import write_synthetic_session


class _UnexpectedFoldLoggerFactory:
    def create(
        self, *, phase: CrossValidationPhase, fold: CrossGroupFold
    ) -> NullExperimentLogger:
        raise AssertionError(f"unavailable plan must not start {phase}: {fold.fold_id}")


class _RecordingFoldLoggerFactory(CrossValidationLoggerFactory):
    def __init__(self) -> None:
        self.loggers: list[NullExperimentLogger] = []

    @override
    def create(
        self, *, phase: CrossValidationPhase, fold: CrossGroupFold
    ) -> NullExperimentLogger:
        logger = NullExperimentLogger(f"{phase}-{fold.fold_id}")
        self.loggers.append(logger)
        return logger


class _FailingSummaryLogger(NullExperimentLogger):
    @override
    def log_params(self, params: Mapping[str, Scalar]) -> None:
        del params
        raise RuntimeError("summary write failed")

    @override
    def set_tags(self, tags: Mapping[str, Scalar]) -> None:
        del tags
        raise RuntimeError("tag write failed")


class TestCrossValidationConfig:
    def test_composes_hydra_config_and_resolves_paths(self, tmp_path: Path):
        with initialize_config_module(
            config_module="pcbasm.pasting.paste_volume.conf", version_base="1.3"
        ):
            raw = compose(
                config_name="cross_validate",
                overrides=[
                    "data.manifest=datasets/composite.json",
                    "cross_validation.dimension=paste_lot",
                    "cross_validation.output_directory=outputs/cross",
                ],
            )
        resolved = OmegaConf.to_container(raw, resolve=True, throw_on_missing=True)
        assert isinstance(resolved, dict)

        config, logger = cross_validation_config_from_mapping(
            cast(dict[str, object], resolved),
            base_directory=tmp_path,
            hydra_resolved_yaml=(
                "logger:\n"
                "  tracking_uri: https://user:password@example.invalid/mlflow?token=x\n"
            ),
            hydra_overrides=(
                "logger.tracking_uri=https://user:password@example.invalid/mlflow?token=x",
            ),
        )

        assert config.dimension == "paste_lot"
        assert (
            config.training.data.manifest
            == (tmp_path / "datasets/composite.json").resolve()
        )
        assert config.output_directory == (tmp_path / "outputs/cross").resolve()
        assert logger.experiment_name == "paste-volume"
        assert config.training.hydra_resolved_yaml is not None
        assert "password" not in config.training.hydra_resolved_yaml
        assert "token=x" not in config.training.hydra_resolved_yaml
        assert "password" not in config.training.hydra_overrides[0]
        assert "token=x" not in config.training.hydra_overrides[0]

    def test_rejects_unknown_top_level_key(self, tmp_path: Path):
        with pytest.raises(ValueError, match="unknown cross-validation"):
            cross_validation_config_from_mapping(
                {"unexpected": True}, base_directory=tmp_path
            )

    @pytest.mark.parametrize(
        ("override", "message"),
        (
            ("cross_validation.dimension=session", "dimension is invalid"),
            ("cross_validation.seed=true", "seed must be a non-negative integer"),
            (
                "cross_validation.evaluation_compile_enabled=invalid",
                "evaluation_compile_enabled must be bool",
            ),
        ),
    )
    def test_rejects_invalid_cross_validation_values(
        self, tmp_path: Path, override: str, message: str
    ):
        with initialize_config_module(
            config_module="pcbasm.pasting.paste_volume.conf", version_base="1.3"
        ):
            raw = compose(
                config_name="cross_validate",
                overrides=["data.manifest=dataset.json", override],
            )
        resolved = OmegaConf.to_container(raw, resolve=True, throw_on_missing=True)
        assert isinstance(resolved, dict)

        with pytest.raises(ValueError, match=message):
            cross_validation_config_from_mapping(
                cast(dict[str, object], resolved), base_directory=tmp_path
            )


class TestFormalCrossValidation:
    def test_records_unavailable_cross_group_result_as_release_blocking(
        self, tmp_path: Path
    ):
        dataset_root = tmp_path / "datasets"
        write_synthetic_session(dataset_root, "only-session")
        output = tmp_path / "cross-output"
        training = TrainConfig(
            data=DataConfig(roots=(dataset_root,)),
            checkpoint=CheckpointConfig(directory=tmp_path / "unused"),
            trainer=TrainerConfig(max_epochs=1, compile_enabled=False),
            repository_root=Path(__file__).parents[4],
            hydra_resolved_yaml=(
                "logger:\n"
                "  tracking_uri: https://example.invalid/mlflow\n"
                "cross_validation:\n"
                "  dimension: machine\n"
            ),
            hydra_overrides=(
                "logger.tracking_uri=https://example.invalid/mlflow",
                "cross_validation.dimension=machine",
            ),
        )
        logger_config = LoggerConfig(
            tracking_uri=(
                "https://user:password@example.invalid/mlflow?token=secret#fragment"
            )
        )
        summary_logger = NullExperimentLogger("summary-run")

        execution = run_formal_cross_validation(
            config=CrossValidationConfig(
                training=training,
                output_directory=output,
                dimension="machine",
            ),
            logger_config=logger_config,
            logger_factory=_UnexpectedFoldLoggerFactory(),
            summary_logger=summary_logger,
        )

        assert execution.result.available is False
        assert execution.summary_run_id == "summary-run"
        assert execution.resolved_config_path.is_file()
        assert execution.attestation.output_path == execution.result.report_path
        assert execution.attestation.run_kind == "cross-validation-summary"
        assert formal_artifact_attestation_path(execution.result.report_path).is_file()
        assert summary_logger.status == "FINISHED"
        assert summary_logger.tags["release_blocking"] == "true"
        assert (
            summary_logger.tags["cross_validation_report_fingerprint"]
            == execution.result.report_fingerprint
        )
        assert (
            summary_logger.tags["cross_validation_report_sha256"]
            == execution.attestation.output_fingerprint
        )
        assert {path.name for path, _ in summary_logger.artifacts} == {
            "cross-validation-report.json",
            "composite.json",
            "dataset-validation.json",
            "dependencies.json",
            "formal-success.json",
            "git.diff",
            "git.json",
            "hydra-overrides.json",
            "resolved-config.json",
            "resolved-config.yaml",
            "sample-index.json",
        }
        provenance = output / "summary-provenance"
        assert (
            json.loads((provenance / "git.json").read_text(encoding="utf-8"))[
                "untracked_content"
            ]
            == "[omitted at persistence boundary]"
        )
        for path in (
            output / "resolved-config.json",
            provenance / "resolved-config.yaml",
            provenance / "hydra-overrides.json",
        ):
            persisted = path.read_text(encoding="utf-8")
            assert "password" not in persisted
            assert "token=secret" not in persisted

    def test_logs_each_available_fold_split_and_evaluation_report(self, tmp_path: Path):
        dataset_root = tmp_path / "datasets"
        for index in range(4):
            write_synthetic_session(
                dataset_root,
                f"session-{index}",
                machine_id=f"machine-{index // 2}",
                pad_count=1,
                width=40,
                height=40,
                content_seed=index,
            )
        output = tmp_path / "cross-output"
        training = TrainConfig(
            data=DataConfig(
                roots=(dataset_root,),
                constraints=ImageConstraints(
                    min_size=32,
                    max_size=64,
                    max_pixels=4_096,
                    stride=32,
                ),
                augmentation=AugmentationConfig(enabled=False),
                max_batch_pixels=8_192,
                max_batch_size=2,
            ),
            checkpoint=CheckpointConfig(directory=tmp_path / "unused"),
            model=PasteVolumeModelConfig(
                stem_channels=(4, 4, 4),
                stage_channels=(4, 4, 4),
                blocks_per_stage=(1, 1, 1),
                group_norm_groups=1,
                hidden_features=4,
            ),
            trainer=TrainerConfig(
                device="cpu",
                max_epochs=1,
                max_steps=1,
                early_stopping_patience=1,
                scheduler_patience=1,
                compile_enabled=False,
                deterministic=True,
            ),
            repository_root=Path(__file__).parents[4],
        )
        summary_logger = NullExperimentLogger("summary-run")
        factory = _RecordingFoldLoggerFactory()

        execution = run_formal_cross_validation(
            config=CrossValidationConfig(
                training=training,
                output_directory=output,
                dimension="machine",
                evaluation_device="cpu",
                evaluation_compile_enabled=False,
            ),
            logger_config=LoggerConfig(),
            logger_factory=factory,
            summary_logger=summary_logger,
        )

        assert execution.result.available
        assert len(factory.loggers) == 4
        logged = set(summary_logger.artifacts)
        for fold in execution.result.folds:
            artifact_path = f"cross-validation/folds/{fold.fold_id}"
            assert (fold.split_path, artifact_path) in logged
            assert (fold.evaluation_report_path, artifact_path) in logged
            assert (fold.diagnostic_report_path, artifact_path) in logged

    def test_closes_started_summary_run_when_logging_fails(self, tmp_path: Path):
        dataset_root = tmp_path / "datasets"
        write_synthetic_session(dataset_root, "only-session")
        training = TrainConfig(
            data=DataConfig(roots=(dataset_root,)),
            checkpoint=CheckpointConfig(directory=tmp_path / "unused"),
            trainer=TrainerConfig(max_epochs=1, compile_enabled=False),
            repository_root=Path(__file__).parents[4],
        )
        summary_logger = _FailingSummaryLogger("failed-summary")

        with pytest.raises(RuntimeError, match="summary write failed"):
            run_formal_cross_validation(
                config=CrossValidationConfig(
                    training=training,
                    output_directory=tmp_path / "cross-output",
                    dimension="machine",
                ),
                logger_config=LoggerConfig(),
                logger_factory=_UnexpectedFoldLoggerFactory(),
                summary_logger=summary_logger,
            )

        assert summary_logger.status == "FAILED"
        assert not formal_artifact_attestation_path(
            tmp_path / "cross-output" / "cross-validation-report.json"
        ).exists()
