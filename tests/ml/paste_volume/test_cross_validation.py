from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import override

import pytest

from ml.data.image import AugmentationConfig, ImageConstraints
from ml.paste_volume.cross_validation import (
    CrossValidationConfig,
    CrossValidationLoggerFactory,
    load_cross_validation_result,
    run_cross_validation,
)
from ml.paste_volume.model import PasteVolumeModelConfig
from ml.paste_volume.reporting import (
    CrossGroupDimension,
    DiagnosticReport,
)
from ml.paste_volume.train import (
    DataConfig,
    TrainConfig,
    training_protocol_fingerprint,
)
from ml.paste_volume.training import CheckpointConfig, TrainerConfig
from ml.training.experiment import NullExperimentLogger
from tests.ml.paste_volume.support_data import write_synthetic_session


class RecordingLoggerFactory(CrossValidationLoggerFactory):
    def __init__(self) -> None:
        self.created_run_ids: list[str] = []
        self.loggers: dict[str, NullExperimentLogger] = {}

    @override
    def create(self, *, phase, fold):
        run_id = f"{phase}-{fold.fold_id}"
        self.created_run_ids.append(run_id)
        logger = NullExperimentLogger(run_id)
        self.loggers[run_id] = logger
        return logger


def _training_config(dataset_root: Path, output: Path) -> TrainConfig:
    return TrainConfig(
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
        checkpoint=CheckpointConfig(
            directory=output / "unused-primary-run",
            save_interval_steps=100,
            save_interval_seconds=300,
        ),
        model=PasteVolumeModelConfig(
            stem_channels=(4, 4, 4),
            stage_channels=(4, 4, 4),
            blocks_per_stage=(1, 1, 1),
            group_norm_groups=1,
            hidden_features=4,
        ),
        trainer=TrainerConfig(
            device="cpu",
            seed=7,
            learning_rate=1e-3,
            max_epochs=1,
            max_steps=1,
            early_stopping_patience=1,
            scheduler_patience=1,
            compile_enabled=False,
            deterministic=True,
        ),
        repository_root=Path(__file__).parents[3],
    )


class TestCrossValidation:
    @pytest.mark.parametrize(
        ("dimension", "expected_groups"),
        (
            ("machine", {"machine-a", "machine-b"}),
            ("paste_lot", {"paste-a:lot-a", "paste-a:lot-b"}),
            ("nozzle", {f"{0.3:.17g}", f"{0.4:.17g}"}),
        ),
    )
    def test_trains_and_evaluates_an_independent_model_per_held_out_group(
        self,
        tmp_path: Path,
        dimension: CrossGroupDimension,
        expected_groups: set[str],
    ):
        dataset_root = tmp_path / "dataset"
        for index in range(4):
            group_index = index // 2
            write_synthetic_session(
                dataset_root,
                f"session-{index}",
                machine_id=f"machine-{'a' if group_index == 0 else 'b'}",
                paste_lot=f"lot-{'a' if group_index == 0 else 'b'}",
                nozzle_diameter_mm=0.3 + group_index * 0.1,
                pad_count=1,
                width=40,
                height=40,
                content_seed=index,
            )
        output = tmp_path / "cross-validation"
        factory = RecordingLoggerFactory()

        training = replace(
            _training_config(dataset_root, output),
            hydra_resolved_yaml="trainer:\n  max_epochs: 1\n",
            hydra_overrides=("trainer.max_epochs=1",),
        )
        result = run_cross_validation(
            CrossValidationConfig(
                training=training,
                output_directory=output,
                dimension=dimension,
                evaluation_device="cpu",
                evaluation_compile_enabled=False,
            ),
            factory,
        )

        assert result.available
        assert result.reason is None
        assert len(result.folds) == 2
        assert {fold.held_out_group for fold in result.folds} == expected_groups
        assert len({fold.training_run_id for fold in result.folds}) == 2
        assert len({fold.evaluation_run_id for fold in result.folds}) == 2
        assert len(factory.created_run_ids) == 4
        assert all(fold.weights_path.is_file() for fold in result.folds)
        assert all(fold.split_path.is_file() for fold in result.folds)
        assert all(fold.evaluation_report_path.is_file() for fold in result.folds)
        assert all(fold.diagnostic_report_path.is_file() for fold in result.folds)
        assert all(fold.weights_sha256.startswith("sha256:") for fold in result.folds)
        assert all(fold.split_sha256.startswith("sha256:") for fold in result.folds)
        assert all(
            fold.evaluation_report_sha256.startswith("sha256:") for fold in result.folds
        )
        assert all(
            fold.diagnostic_report_sha256.startswith("sha256:") for fold in result.folds
        )
        assert all(
            set(fold.held_out_sample_ids)
            == {prediction.sample_id for prediction in fold.predictions}
            for fold in result.folds
        )
        assert result.report_path.is_file()
        assert result.report_fingerprint.startswith("sha256:")
        assert result.protocol_fingerprint == training_protocol_fingerprint(training)
        assert result.diagnostics is not None
        assert result.diagnostics.overall.sample_count == 4
        assert sum(len(fold.predictions) for fold in result.folds) == 4
        assert load_cross_validation_result(result.report_path) == result
        for fold in result.folds:
            training_directory = output / fold.fold_id / "training"
            assert (training_directory / "resolved-config.yaml").is_file()
            assert json.loads(
                (training_directory / "hydra-overrides.json").read_text(
                    encoding="utf-8"
                )
            ) == {"overrides": ["trainer.max_epochs=1"]}
            evaluation_logger = factory.loggers[fold.evaluation_run_id]
            assert (
                fold.diagnostic_report_path,
                "evaluation",
            ) in evaluation_logger.artifacts
            evaluation_payload = json.loads(
                fold.evaluation_report_path.read_text(encoding="utf-8")
            )
            assert DiagnosticReport.from_dict(evaluation_payload["diagnostics"]) == (
                fold.diagnostics
            )

    def test_persists_unavailable_single_group_without_fallback(self, tmp_path: Path):
        dataset_root = tmp_path / "dataset"
        write_synthetic_session(dataset_root, "only", machine_id="same", pad_count=1)
        output = tmp_path / "cross-validation"
        factory = RecordingLoggerFactory()

        result = run_cross_validation(
            CrossValidationConfig(
                training=_training_config(dataset_root, output),
                output_directory=output,
                dimension="machine",
                evaluation_device="cpu",
                evaluation_compile_enabled=False,
            ),
            factory,
        )

        assert factory.created_run_ids == []
        assert not result.available
        assert "2種類未満" in str(result.reason)
        assert result.folds == ()
        assert result.diagnostics is None
        assert result.report_path.is_file()
        assert load_cross_validation_result(result.report_path) == result

        second_output = tmp_path / "cross-validation-second"
        second = run_cross_validation(
            CrossValidationConfig(
                training=_training_config(dataset_root, second_output),
                output_directory=second_output,
                dimension="machine",
                evaluation_device="cpu",
                evaluation_compile_enabled=False,
            ),
            RecordingLoggerFactory(),
        )
        assert second.report_path.read_bytes() == result.report_path.read_bytes()

    def test_rejects_protocol_fingerprint_not_derived_from_training_config(
        self, tmp_path: Path
    ):
        dataset_root = tmp_path / "dataset"
        write_synthetic_session(dataset_root, "only", machine_id="same", pad_count=1)
        training = _training_config(dataset_root, tmp_path / "output")

        with pytest.raises(ValueError, match="protocol_fingerprint"):
            run_cross_validation(
                CrossValidationConfig(
                    training=training,
                    output_directory=tmp_path / "cross-validation",
                    dimension="machine",
                    evaluation_device="cpu",
                    evaluation_compile_enabled=False,
                    protocol_fingerprint="sha256:" + "a" * 64,
                ),
                RecordingLoggerFactory(),
            )

    def test_rejects_tampered_report_fingerprint(self, tmp_path: Path):
        dataset_root = tmp_path / "dataset"
        write_synthetic_session(dataset_root, "only", machine_id="same", pad_count=1)
        result = run_cross_validation(
            CrossValidationConfig(
                training=_training_config(dataset_root, tmp_path / "output"),
                output_directory=tmp_path / "output",
                dimension="machine",
                evaluation_device="cpu",
                evaluation_compile_enabled=False,
            ),
            RecordingLoggerFactory(),
        )
        payload = json.loads(result.report_path.read_text(encoding="utf-8"))
        payload["reason"] = "tampered"
        result.report_path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ValueError, match="fingerprint"):
            load_cross_validation_result(result.report_path)

    def test_refuses_nonempty_output_and_resume_checkpoint(self, tmp_path: Path):
        dataset_root = tmp_path / "dataset"
        write_synthetic_session(dataset_root, "a")
        output = tmp_path / "cross-validation"
        output.mkdir()
        (output / "existing").write_text("do not overwrite", encoding="utf-8")
        factory = RecordingLoggerFactory()

        with pytest.raises(FileExistsError, match="空"):
            run_cross_validation(
                CrossValidationConfig(
                    training=_training_config(dataset_root, output),
                    output_directory=output,
                    dimension="machine",
                ),
                factory,
            )

        checkpoint = tmp_path / "resume" / "latest.ckpt"
        checkpoint.parent.mkdir()
        checkpoint.touch()
        base = _training_config(dataset_root, output)
        with pytest.raises(ValueError, match="resume checkpoint"):
            CrossValidationConfig(
                training=TrainConfig(
                    data=base.data,
                    checkpoint=CheckpointConfig(
                        directory=checkpoint.parent,
                        resume_checkpoint=checkpoint,
                    ),
                    model=base.model,
                    trainer=base.trainer,
                    repository_root=base.repository_root,
                ),
                output_directory=tmp_path / "other",
                dimension="machine",
            )
