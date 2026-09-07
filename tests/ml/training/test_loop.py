"""Trainer の epoch ループ・中断・resume・終了状態の公開契約."""

from __future__ import annotations

import os
import signal
import threading
from pathlib import Path
from typing import override

import attrs
import pytest
import torch

from ml.data.split import SplitName
from ml.evaluation.compile_parity import CompileOptions
from ml.model.blocks import ImageEncoder, ImageEncoderConfig
from ml.model.heads import (
    GaussianHeadConfig,
    GaussianImageRegressor,
    GaussianRegressionHead,
)
from ml.training.checkpoint import CheckpointStore, TrainingCheckpoint
from ml.training.loop import (
    NonFiniteLossError,
    TerminationSignals,
    Trainer,
    TrainerConfig,
    TrainingOutcome,
)
from ml.training.task import GaussianBatch, GaussianObservation, StepResult
from tests.ml.support import (
    ENCODER_CONFIG,
    RecordingExperimentLogger,
    SyntheticDatasetOptions,
    SyntheticRegressionData,
    SyntheticRegressionTask,
    SyntheticTaskOptions,
    build_synthetic_model,
)

MONITOR = "relative_error_score"
DEVICE = torch.device("cpu")

# 12 sample のうち train が 9 件、batch size 3 なので 1 epoch は 3 batch
TRAIN_BATCHES_PER_EPOCH = 3

# Resume 互換性の判定に使う fingerprint へ入る、学習の意味論を決めるフィールド。
FINGERPRINT_FIELDS = frozenset(
    {
        "max_epochs",
        "monitor",
        "mode",
        "max_steps",
        "learning_rate",
        "weight_decay",
        "gradient_accumulation",
        "gradient_clip_norm",
        "early_stopping_patience",
        "early_stopping_minimum_delta",
        "scheduler_factor",
        "scheduler_patience",
        "automatic_mixed_precision_enabled",
        "compile_enabled",
        "compile_options",
        "deterministic",
        "seed",
    }
)

# Fingerprint から外すフィールド。
#
# 除外してよいのは「その run 限りの時間予算であって、学習の意味論を変えないもの」だけ。
#
# 含めてしまうと deadline で中断した run を新しい時間予算で resume できなくなり、
# ``resume_rejection`` が config 不一致として拒否してしまう。
FINGERPRINT_EXCLUDED_FIELDS = frozenset(
    {
        "deadline_seconds",
        "finalization_grace_seconds",
        "checkpoint_interval_steps",
        "checkpoint_interval_seconds",
    }
)

# 各フィールドの「既定と異なる値」。上の 2 集合から parametrize を導出するために使う。
FIELD_CHANGES: dict[str, object] = {
    "max_epochs": 5,
    "monitor": "negative_log_likelihood",
    "mode": "max",
    "max_steps": 3,
    "learning_rate": 0.5,
    "weight_decay": 0.5,
    "gradient_accumulation": 2,
    "gradient_clip_norm": 2.0,
    "early_stopping_patience": 1,
    "early_stopping_minimum_delta": 0.5,
    "scheduler_factor": 0.25,
    "scheduler_patience": 1,
    "automatic_mixed_precision_enabled": True,
    "compile_enabled": True,
    "compile_options": CompileOptions(backend="eager"),
    "deterministic": False,
    "seed": 99,
    "deadline_seconds": 123.0,
    "finalization_grace_seconds": 1.0,
    "checkpoint_interval_steps": 7,
    "checkpoint_interval_seconds": 1.0,
}


class _SignallingTask(SyntheticRegressionTask):
    """指定した学習 step の途中で自分自身へ SIGTERM を送る task.

    時間に依存せず、group 境界での停止を決定論的に再現するために使う。
    """

    def __init__(
        self,
        model: GaussianImageRegressor,
        options: SyntheticTaskOptions,
        *,
        signal_at_step: int,
    ) -> None:
        super().__init__(model, options)
        self._signal_at_step = signal_at_step

    @override
    def training_step(self, batch: GaussianBatch) -> StepResult[GaussianObservation]:
        should_signal = self.training_step_count == self._signal_at_step
        result = super().training_step(batch)
        if should_signal:
            os.kill(os.getpid(), signal.SIGTERM)
        return result


class _ReversedPlanData(SyntheticRegressionData):
    """Batch 計画だけを反転させ、resume の計画不一致を作る dataset."""

    @override
    def plan_epoch(
        self, *, split: SplitName, epoch: int
    ) -> tuple[tuple[str, ...], ...]:
        return tuple(reversed(super().plan_epoch(split=split, epoch=epoch)))


class _EmptyValidationData(SyntheticRegressionData):
    """Validation の batch が 1 個も無い dataset."""

    @override
    def plan_epoch(
        self, *, split: SplitName, epoch: int
    ) -> tuple[tuple[str, ...], ...]:
        if split == "validation":
            return ()
        return super().plan_epoch(split=split, epoch=epoch)


def _config(**overrides: object) -> TrainerConfig:
    values: dict[str, object] = {
        "max_epochs": 2,
        "monitor": MONITOR,
        "mode": "min",
        "seed": 0,
        "deterministic": True,
        "checkpoint_interval_steps": 1,
    }
    values.update(overrides)
    return TrainerConfig(**values)  # pyright: ignore[reportArgumentType]


def _wide_model() -> GaussianImageRegressor:
    """Stem を 1 段増やして state_dict のキー集合を変えた model."""

    torch.manual_seed(1)
    config = ImageEncoderConfig(
        input_channels=ENCODER_CONFIG.input_channels,
        stem_channels=(8, 8),
        stem_strides=(2, 1),
        stage_channels=ENCODER_CONFIG.stage_channels,
        stage_strides=ENCODER_CONFIG.stage_strides,
        blocks_per_stage=ENCODER_CONFIG.blocks_per_stage,
        group_norm_groups=ENCODER_CONFIG.group_norm_groups,
    )
    encoder = ImageEncoder(config)
    head = GaussianRegressionHead(
        GaussianHeadConfig(input_features=encoder.output_features, hidden_features=8)
    )
    return GaussianImageRegressor(encoder, head)


def _trainer(
    directory: Path,
    *,
    config: TrainerConfig | None = None,
    task: SyntheticRegressionTask | None = None,
    data: SyntheticRegressionData | None = None,
    logger: RecordingExperimentLogger | None = None,
) -> tuple[Trainer[GaussianBatch, GaussianObservation], RecordingExperimentLogger]:
    recorder = logger or RecordingExperimentLogger()
    trainer = Trainer(
        task
        or SyntheticRegressionTask(
            build_synthetic_model(seed=1), SyntheticTaskOptions()
        ),
        data or SyntheticRegressionData(SyntheticDatasetOptions()),
        config=config or _config(),
        store=CheckpointStore(directory),
        logger=recorder,
        device=DEVICE,
    )
    return trainer, recorder


def _state_dict(store: CheckpointStore, role: str) -> dict[str, torch.Tensor]:
    checkpoint, reason = store.load(role)  # pyright: ignore[reportArgumentType]

    assert reason is None
    assert checkpoint is not None
    return dict(checkpoint.model_state)


def _assert_same_weights(
    left: dict[str, torch.Tensor], right: dict[str, torch.Tensor]
) -> None:
    assert sorted(left) == sorted(right)
    for key in left:
        # deterministic=True の CPU 実行なので bit 一致を要求する
        assert torch.allclose(left[key], right[key], rtol=0.0, atol=0.0), key


class TestTrainerConfig:
    """設定は使う前に理由つきで検証でき、fingerprint は内容から決まる."""

    def test_valid_config_has_no_reason(self):
        assert _config().validate() is None

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"max_epochs": 0}, "max_epochs"),
            ({"gradient_accumulation": 0}, "gradient_accumulation"),
            ({"gradient_clip_norm": 0.0}, "gradient_clip_norm"),
            ({"early_stopping_patience": -1}, "early_stopping_patience"),
            ({"monitor": ""}, "monitor"),
            ({"finalization_grace_seconds": -1.0}, "finalization_grace_seconds"),
        ],
    )
    def test_invalid_values_are_rejected(
        self, overrides: dict[str, object], expected: str
    ):
        reason = _config(**overrides).validate()

        assert reason is not None
        assert expected in reason

    def test_fingerprint_is_content_addressed(self):
        assert _config().fingerprint.startswith("sha256:")
        assert _config().fingerprint == _config().fingerprint
        assert _config().fingerprint != _config(learning_rate=0.5).fingerprint

    def test_every_field_is_classified_as_semantic_or_time_budget(self):
        """新しいフィールドを必ずどちらかへ分類させるための pin.

        分類漏れは意味論フィールドを fingerprint の外へ落とし、resume 拒否をすり抜けさせる。
        """

        names = {field.name for field in attrs.fields(TrainerConfig)}

        assert FINGERPRINT_FIELDS & FINGERPRINT_EXCLUDED_FIELDS == frozenset()
        assert FINGERPRINT_FIELDS | FINGERPRINT_EXCLUDED_FIELDS == names

    def test_every_field_has_a_change_case(self):
        """分類したフィールドすべてに、既定と異なる値の検証がある."""

        assert set(FIELD_CHANGES) == FINGERPRINT_FIELDS | FINGERPRINT_EXCLUDED_FIELDS

    @pytest.mark.parametrize("field_name", sorted(FINGERPRINT_EXCLUDED_FIELDS))
    def test_time_budget_fields_do_not_change_the_fingerprint(self, field_name: str):
        changed = _config(**{field_name: FIELD_CHANGES[field_name]})

        assert getattr(changed, field_name) != getattr(_config(), field_name)
        assert changed.fingerprint == _config().fingerprint

    @pytest.mark.parametrize("field_name", sorted(FINGERPRINT_FIELDS))
    def test_semantic_fields_change_the_fingerprint(self, field_name: str):
        changed = _config(**{field_name: FIELD_CHANGES[field_name]})

        assert getattr(changed, field_name) != getattr(_config(), field_name)
        assert changed.fingerprint != _config().fingerprint

    def test_trainer_rejects_an_invalid_config(self, tmp_path: Path):
        with pytest.raises(ValueError) as exception:
            _trainer(tmp_path, config=_config(max_epochs=0))

        assert "max_epochs" in str(exception.value)


class TestTrainerFullRun:
    """通しで走り切った run の checkpoint と記録."""

    def test_two_epochs_finish_with_max_epochs(self, tmp_path: Path):
        trainer, _ = _trainer(tmp_path)
        store = CheckpointStore(tmp_path)

        outcome = trainer.run()

        assert isinstance(outcome, TrainingOutcome)
        assert outcome.stop_reason == "max_epochs"
        assert outcome.epochs_completed == 2
        assert outcome.global_step == 2 * TRAIN_BATCHES_PER_EPOCH
        assert outcome.best_monitor_value is not None
        assert outcome.best_epoch in (0, 1)
        assert outcome.best_checkpoint_path == store.path_for("best")
        assert outcome.final_checkpoint_path == store.path_for("final")
        assert MONITOR in outcome.last_validation_metrics
        assert outcome.elapsed_seconds >= 0.0

    def test_final_checkpoint_holds_the_best_weights(self, tmp_path: Path):
        trainer, _ = _trainer(tmp_path)
        store = CheckpointStore(tmp_path)

        trainer.run()

        _assert_same_weights(_state_dict(store, "final"), _state_dict(store, "best"))

    def test_best_checkpoint_records_the_best_epoch(self, tmp_path: Path):
        trainer, _ = _trainer(tmp_path)
        store = CheckpointStore(tmp_path)

        outcome = trainer.run()

        best, reason = store.load("best")
        assert reason is None
        assert best is not None
        assert best.role == "best"
        assert best.progress.epoch == outcome.best_epoch
        assert best.selection.best_epoch == outcome.best_epoch
        assert best.selection.best_value == outcome.best_monitor_value

    def test_run_is_ended_once_as_finished(self, tmp_path: Path):
        trainer, logger = _trainer(tmp_path)

        trainer.run()

        assert logger.end_call_count == 1
        assert logger.status == "FINISHED"
        assert logger.start_call_count == 1
        assert logger.run_kinds == ["training"]

    def test_metrics_are_logged_once_per_epoch_on_the_global_step_axis(
        self, tmp_path: Path
    ):
        trainer, logger = _trainer(tmp_path)

        trainer.run()

        steps = [step for step, _ in logger.metrics]
        assert steps == [TRAIN_BATCHES_PER_EPOCH, 2 * TRAIN_BATCHES_PER_EPOCH]
        assert all(f"validation/{MONITOR}" in values for _, values in logger.metrics)
        assert len(logger.metrics_for("learning_rate")) == 2
        assert len(logger.metrics_for("epoch_seconds")) == 2
        assert len(logger.metrics_for("train_samples_per_second")) == 2

    def test_configuration_is_logged_as_params(self, tmp_path: Path):
        trainer, logger = _trainer(tmp_path)

        trainer.run()

        assert logger.params["max_epochs"] == 2
        assert logger.params["monitor"] == MONITOR
        assert logger.params["config_fingerprint"] == _config().fingerprint
        assert logger.params["dataset_fingerprint"] == (
            SyntheticRegressionData(SyntheticDatasetOptions()).dataset_fingerprint
        )

    def test_time_budget_fields_are_tags_not_params(self, tmp_path: Path):
        """時間予算は MLflow の param ではなく tag へ載せる.

        param は run 内で不変なので、別の時間予算で resume すると記録が必ず失敗する。
        """

        trainer, logger = _trainer(tmp_path)

        trainer.run()

        for name in sorted(FINGERPRINT_EXCLUDED_FIELDS):
            assert name not in logger.params
            assert logger.tags[name] == str(getattr(_config(), name))

    def test_every_semantic_field_is_logged_as_a_param(self, tmp_path: Path):
        trainer, logger = _trainer(tmp_path)

        trainer.run()

        for name in sorted(FINGERPRINT_FIELDS - {"compile_options"}):
            assert name in logger.params

    def test_final_checkpoint_is_logged_as_an_artifact(self, tmp_path: Path):
        trainer, logger = _trainer(tmp_path)
        store = CheckpointStore(tmp_path)

        trainer.run()

        assert store.path_for("final") in [path for path, _ in logger.artifacts]


class TestTrainerInterruptionParity:
    """中断して resume しても、通し実行と同じ重みに到達する."""

    def _reference_weights(self, tmp_path: Path) -> tuple[dict[str, torch.Tensor], int]:
        trainer, _ = _trainer(tmp_path / "reference")
        outcome = trainer.run()

        assert outcome.stop_reason == "max_epochs"
        return _state_dict(CheckpointStore(tmp_path / "reference"), "final"), (
            outcome.global_step
        )

    @pytest.mark.parametrize("gradient_accumulation", [1, 2])
    def test_signal_stops_at_a_group_boundary(
        self, tmp_path: Path, gradient_accumulation: int
    ):
        directory = tmp_path / "interrupted"
        task = _SignallingTask(
            build_synthetic_model(seed=1), SyntheticTaskOptions(), signal_at_step=2
        )
        trainer, logger = _trainer(
            directory,
            task=task,
            config=_config(gradient_accumulation=gradient_accumulation),
        )

        outcome = trainer.run()

        store = CheckpointStore(directory)
        checkpoint, reason = store.load("latest")
        assert outcome.stop_reason == "signal"
        assert logger.end_call_count == 1
        assert logger.status == "KILLED"
        # 中断した epoch は validation を走らせないので metric が 1 件も出ない
        assert logger.metrics == []
        assert reason is None
        assert checkpoint is not None
        next_index = checkpoint.progress.next_batch_index
        assert next_index % gradient_accumulation == 0
        assert 0 < next_index < TRAIN_BATCHES_PER_EPOCH

    def test_resume_reaches_the_uninterrupted_weights(self, tmp_path: Path):
        expected_weights, expected_step = self._reference_weights(tmp_path)
        directory = tmp_path / "interrupted"
        interrupted, _ = _trainer(
            directory,
            task=_SignallingTask(
                build_synthetic_model(seed=1), SyntheticTaskOptions(), signal_at_step=2
            ),
        )
        first = interrupted.run()

        store = CheckpointStore(directory)
        resumed, _ = _trainer(directory)
        second = resumed.run(resume_from=store.path_for("latest"))

        assert first.stop_reason == "signal"
        assert second.stop_reason == "max_epochs"
        # rewind した group を数え直さないので、更新回数は通し実行と一致する
        assert second.global_step == expected_step
        _assert_same_weights(_state_dict(store, "final"), expected_weights)

    def test_resume_reproduces_the_validation_metrics(self, tmp_path: Path):
        reference_trainer, _ = _trainer(tmp_path / "reference")
        expected = dict(reference_trainer.run().last_validation_metrics)

        directory = tmp_path / "interrupted"
        interrupted, _ = _trainer(
            directory,
            task=_SignallingTask(
                build_synthetic_model(seed=1), SyntheticTaskOptions(), signal_at_step=2
            ),
        )
        interrupted.run()
        resumed, _ = _trainer(directory)
        outcome = resumed.run(resume_from=CheckpointStore(directory).path_for("latest"))

        assert dict(outcome.last_validation_metrics) == expected


class TestTrainerDeadline:
    """Deadline は例外ではなく正常な停止条件として扱う."""

    def test_immediate_deadline_finishes_without_a_best_checkpoint(
        self, tmp_path: Path
    ):
        trainer, logger = _trainer(tmp_path, config=_config(deadline_seconds=1e-9))
        store = CheckpointStore(tmp_path)

        outcome = trainer.run()

        checkpoint, reason = store.load("latest")
        assert outcome.stop_reason == "deadline"
        assert outcome.best_checkpoint_path is None
        assert outcome.best_epoch is None
        assert logger.status == "FINISHED"
        assert logger.end_call_count == 1
        assert reason is None
        assert checkpoint is not None
        assert checkpoint.progress.global_step == 0

    def test_no_deadline_runs_to_completion(self, tmp_path: Path):
        trainer, _ = _trainer(tmp_path, config=_config(deadline_seconds=None))

        outcome = trainer.run()

        assert outcome.stop_reason == "max_epochs"


class TestTrainerCompileSeam:
    """Compile しても checkpoint のキーは compile なしと完全一致する."""

    def test_compiled_run_keeps_the_plain_state_dict_keys(self, tmp_path: Path):
        plain, _ = _trainer(tmp_path / "plain", config=_config(max_epochs=1))
        plain.run()
        compiled, _ = _trainer(
            tmp_path / "compiled",
            config=_config(
                max_epochs=1,
                compile_enabled=True,
                compile_options=CompileOptions(backend="eager"),
            ),
        )

        compiled.run()

        assert sorted(
            _state_dict(CheckpointStore(tmp_path / "compiled"), "latest")
        ) == (sorted(_state_dict(CheckpointStore(tmp_path / "plain"), "latest")))


class TestTrainerSeeding:
    """Run の再現性は ``TrainerConfig.seed`` だけで決まる.

    run に入る前の global RNG がどうなっていても結果が変わってはいけない。

    task を組み立てる側が自前で seed を張っていると、Trainer が seed を設定
    しなくても結果が揃ってしまい、seeding が壊れても気付けなくなる。
    """

    def _final_weights(
        self, directory: Path, *, seed: int, ambient_seed: int
    ) -> dict[str, torch.Tensor]:
        trainer, _ = _trainer(directory, config=_config(seed=seed))
        # run() が seed_everything を呼ぶまでの global RNG を意図的にずらす
        torch.manual_seed(ambient_seed)
        trainer.run()
        return _state_dict(CheckpointStore(directory), "final")

    def test_same_seed_ignores_the_ambient_random_state(self, tmp_path: Path):
        first = self._final_weights(tmp_path / "first", seed=3, ambient_seed=11)
        second = self._final_weights(tmp_path / "second", seed=3, ambient_seed=97)

        _assert_same_weights(first, second)

    def test_different_seeds_reach_different_weights(self, tmp_path: Path):
        first = self._final_weights(tmp_path / "first", seed=3, ambient_seed=11)
        second = self._final_weights(tmp_path / "second", seed=4, ambient_seed=11)

        assert sorted(first) == sorted(second)
        assert any(
            not torch.allclose(first[key], second[key], rtol=0.0, atol=0.0)
            for key in first
        )


class TestTrainerNonFiniteLoss:
    """非有限 loss は緊急 checkpoint を残して失敗させる."""

    def test_emergency_checkpoint_is_written_and_latest_is_untouched(
        self, tmp_path: Path
    ):
        task = SyntheticRegressionTask(
            build_synthetic_model(seed=1), SyntheticTaskOptions(non_finite_at_step=2)
        )
        trainer, logger = _trainer(tmp_path, task=task)
        store = CheckpointStore(tmp_path)

        with pytest.raises(NonFiniteLossError):
            trainer.run()

        emergency, emergency_reason = store.load("emergency")
        latest, latest_reason = store.load("latest")
        assert emergency_reason is None
        assert emergency is not None
        assert emergency.role == "emergency"
        assert latest_reason is None
        assert latest is not None
        # 非有限化した group は commit されていないので latest は手前の状態のまま
        assert latest.progress.global_step == 2
        assert logger.end_call_count == 1
        assert logger.status == "FAILED"
        assert logger.tags != {}

    def test_emergency_checkpoint_is_not_resumable(self, tmp_path: Path):
        task = SyntheticRegressionTask(
            build_synthetic_model(seed=1), SyntheticTaskOptions(non_finite_at_step=2)
        )
        trainer, _ = _trainer(tmp_path, task=task)
        store = CheckpointStore(tmp_path)
        with pytest.raises(NonFiniteLossError):
            trainer.run()
        checkpoint, _ = store.load("emergency")

        assert checkpoint is not None
        resumed, _ = _trainer(tmp_path)
        with pytest.raises(ValueError) as exception:
            resumed.run(resume_from=store.path_for("emergency"))

        assert "emergency" in str(exception.value)


class TestTrainerResumeRejections:
    """Resume してはいけない組み合わせは理由つきの ValueError で止める."""

    def _interrupted_latest(self, directory: Path) -> Path:
        trainer, _ = _trainer(
            directory,
            task=_SignallingTask(
                build_synthetic_model(seed=1), SyntheticTaskOptions(), signal_at_step=2
            ),
        )
        trainer.run()
        return CheckpointStore(directory).path_for("latest")

    def test_dataset_mismatch_is_rejected(self, tmp_path: Path):
        latest = self._interrupted_latest(tmp_path)
        resumed, _ = _trainer(
            tmp_path, data=SyntheticRegressionData(SyntheticDatasetOptions(seed=99))
        )

        with pytest.raises(ValueError) as exception:
            resumed.run(resume_from=latest)

        assert "dataset" in str(exception.value)

    def test_config_mismatch_is_rejected(self, tmp_path: Path):
        latest = self._interrupted_latest(tmp_path)
        resumed, _ = _trainer(tmp_path, config=_config(learning_rate=0.5))

        with pytest.raises(ValueError) as exception:
            resumed.run(resume_from=latest)

        assert "config" in str(exception.value)

    def test_run_id_mismatch_is_rejected(self, tmp_path: Path):
        latest = self._interrupted_latest(tmp_path)
        logger = RecordingExperimentLogger(run_id="another-run")
        resumed, _ = _trainer(tmp_path, logger=logger)

        with pytest.raises(ValueError) as exception:
            resumed.run(resume_from=latest)

        assert "run_id" in str(exception.value)
        assert logger.end_call_count == 1
        assert logger.status == "FAILED"

    def test_model_state_key_mismatch_is_rejected(self, tmp_path: Path):
        latest = self._interrupted_latest(tmp_path)
        resumed, _ = _trainer(
            tmp_path,
            task=SyntheticRegressionTask(_wide_model(), SyntheticTaskOptions()),
        )

        with pytest.raises(ValueError) as exception:
            resumed.run(resume_from=latest)

        assert "_encoder._stem.1" in str(exception.value)

    def test_batch_plan_mismatch_is_rejected(self, tmp_path: Path):
        latest = self._interrupted_latest(tmp_path)
        resumed, _ = _trainer(
            tmp_path, data=_ReversedPlanData(SyntheticDatasetOptions())
        )

        with pytest.raises(ValueError) as exception:
            resumed.run(resume_from=latest)

        assert "batch_plan" in str(exception.value)


class TestTrainerMonitor:
    """Monitor が取れない run は理由つきで止める."""

    def test_unknown_monitor_key_is_rejected(self, tmp_path: Path):
        trainer, _ = _trainer(tmp_path, config=_config(monitor="not-a-metric"))

        with pytest.raises(ValueError) as exception:
            trainer.run()

        assert "not-a-metric" in str(exception.value)

    def test_empty_validation_is_rejected(self, tmp_path: Path):
        trainer, _ = _trainer(
            tmp_path, data=_EmptyValidationData(SyntheticDatasetOptions())
        )

        with pytest.raises(ValueError) as exception:
            trainer.run()

        assert MONITOR in str(exception.value)


class TestTrainerEdgeCases:
    """境界的な設定でも停止理由と checkpoint が破綻しない."""

    def test_accumulation_that_does_not_divide_the_epoch(self, tmp_path: Path):
        trainer, _ = _trainer(tmp_path, config=_config(gradient_accumulation=2))

        outcome = trainer.run()

        assert outcome.stop_reason == "max_epochs"
        assert outcome.epochs_completed == 2

    def test_single_batch_epoch(self, tmp_path: Path):
        trainer, _ = _trainer(
            tmp_path,
            config=_config(gradient_accumulation=2),
            data=SyntheticRegressionData(SyntheticDatasetOptions(sample_count=2)),
        )

        outcome = trainer.run()

        assert outcome.stop_reason == "max_epochs"
        assert outcome.epochs_completed == 2

    def test_max_steps_stops_inside_the_first_epoch(self, tmp_path: Path):
        trainer, _ = _trainer(tmp_path, config=_config(max_steps=1))

        outcome = trainer.run()

        assert outcome.stop_reason == "max_steps"
        assert outcome.global_step == 1
        assert outcome.epochs_completed == 0

    def test_zero_patience_stops_after_the_first_epoch(self, tmp_path: Path):
        trainer, _ = _trainer(tmp_path, config=_config(early_stopping_patience=0))

        outcome = trainer.run()

        assert outcome.stop_reason == "early_stopping"
        assert outcome.epochs_completed == 1

    def test_never_improving_run_keeps_the_first_epoch_as_best(self, tmp_path: Path):
        # 到達不能な minimum_delta を置くと 2 epoch 目以降は必ず非改善になる
        trainer, _ = _trainer(
            tmp_path,
            config=_config(
                early_stopping_minimum_delta=1e9, early_stopping_patience=15
            ),
        )

        outcome = trainer.run()

        assert outcome.stop_reason == "max_epochs"
        assert outcome.best_epoch == 0


class TestTerminationSignals:
    """Signal flag は 1 個だけ持ち、非 main thread では張らない."""

    def test_flag_is_raised_inside_the_context(self):
        with TerminationSignals() as signals:
            assert signals.requested is False

            os.kill(os.getpid(), signal.SIGTERM)

            assert signals.requested is True

    def test_non_main_thread_does_not_install_handlers(self):
        observed: list[bool] = []
        errors: list[BaseException] = []

        def body() -> None:
            try:
                with TerminationSignals() as signals:
                    observed.append(signals.requested)
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        thread = threading.Thread(target=body)
        thread.start()
        thread.join(timeout=10.0)

        assert errors == []
        assert observed == [False]


class TestCheckpointReadableAfterRun:
    """書き出した checkpoint は payload 検証を通って読み戻せる."""

    def test_every_written_checkpoint_loads(self, tmp_path: Path):
        trainer, _ = _trainer(tmp_path)
        store = CheckpointStore(tmp_path)

        trainer.run()

        for role in ("latest", "best", "final"):
            path = store.path_for(role)  # pyright: ignore[reportArgumentType]
            payload = torch.load(path, weights_only=True)
            checkpoint, reason = TrainingCheckpoint.from_payload(payload)
            assert reason is None, role
            assert checkpoint is not None
            assert checkpoint.validate() is None
