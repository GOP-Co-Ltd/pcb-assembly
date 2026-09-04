"""Optimizer group トランザクションと評価 batch 実行の公開契約."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import override

import pytest
import torch
from torch import Tensor, nn
from torch.optim import AdamW

from ml.training.task import (
    GaussianBatch,
    GaussianObservation,
    GaussianRegressionTask,
    StepResult,
    TrainingTask,
)
from ml.training.transaction import (
    OptimizerGroupResult,
    execute_evaluation_batches,
    execute_optimizer_group,
)
from tests.ml.support import (
    SyntheticDatasetOptions,
    SyntheticRegressionData,
    SyntheticRegressionTask,
    SyntheticTaskOptions,
    build_synthetic_model,
)

DEVICE_TYPE = "cpu"
GRADIENT_CLIP_NORM = 1.0


class _InfiniteGradientTask(TrainingTask[Tensor, Tensor]):
    """Loss は有限だが勾配が無限になる、境界検証用の task.

    ``sqrt`` の 0 における微分が無限大になることを使う。
    """

    def __init__(self) -> None:
        self._model = nn.Linear(1, 1, bias=False)
        with torch.no_grad():
            self._model.weight.zero_()

    @property
    @override
    def model(self) -> nn.Module:
        return self._model

    @override
    def training_step(self, batch: Tensor) -> StepResult[Tensor]:
        loss = torch.sqrt(self._model(batch).abs().sum())
        return StepResult(
            loss=loss, observation=loss.detach(), sample_count=int(batch.shape[0])
        )

    @override
    def evaluation_step(self, batch: Tensor) -> Tensor:
        with torch.no_grad():
            return self._model(batch).detach()

    @override
    def reduce(self, observations: Sequence[Tensor]) -> Mapping[str, float]:
        return {}


class _InvalidStepResultTask(GaussianRegressionTask):
    """``StepResult.validate()`` を必ず破る、契約違反の検証用 task."""

    @override
    def training_step(self, batch: GaussianBatch) -> StepResult[GaussianObservation]:
        result = super().training_step(batch)
        return StepResult(
            loss=result.loss, observation=result.observation, sample_count=0
        )


def _batches(count: int = 2) -> tuple[GaussianBatch, ...]:
    data = SyntheticRegressionData(SyntheticDatasetOptions())
    plan = data.plan_epoch(split="train", epoch=0)[:count]
    return tuple(
        data.materialize(sample_ids, split="train", epoch=0, training=False)
        for sample_ids in plan
    )


def _parameters_snapshot[BatchT, ObservationT](
    task: TrainingTask[BatchT, ObservationT],
) -> list[Tensor]:
    return [parameter.detach().clone() for parameter in task.model.parameters()]


def _parameters_changed[BatchT, ObservationT](
    task: TrainingTask[BatchT, ObservationT], before: list[Tensor]
) -> bool:
    return any(
        not torch.equal(snapshot, parameter.detach())
        for snapshot, parameter in zip(before, task.model.parameters(), strict=True)
    )


def _run_group[BatchT, ObservationT](
    task: TrainingTask[BatchT, ObservationT],
    batches: Iterable[BatchT],
    *,
    expected_batch_count: int,
    amp_enabled: bool = False,
    can_commit: Callable[[], bool] | None = None,
    gradient_clip_norm: float = GRADIENT_CLIP_NORM,
) -> tuple[OptimizerGroupResult, tuple[ObservationT, ...]]:
    optimizer = AdamW(
        [parameter for parameter in task.model.parameters() if parameter.requires_grad],
        lr=1e-2,
    )
    return execute_optimizer_group(
        batches,
        expected_batch_count=expected_batch_count,
        task=task,
        optimizer=optimizer,
        gradient_scaler=torch.GradScaler(DEVICE_TYPE, enabled=amp_enabled),
        gradient_clip_norm=gradient_clip_norm,
        device_type=DEVICE_TYPE,
        autocast_enabled=amp_enabled,
        can_commit=can_commit,
    )


def _synthetic_task(**options: object) -> SyntheticRegressionTask:
    return SyntheticRegressionTask(
        build_synthetic_model(seed=5),
        SyntheticTaskOptions(**options),  # pyright: ignore[reportArgumentType]
    )


class TestOptimizerGroupCommit:
    """予定どおり全 batch を処理した group だけが commit される."""

    def test_all_batches_commit_and_update_parameters(self):
        task = _synthetic_task()
        batches = _batches(2)
        before = _parameters_snapshot(task)

        result, observations = _run_group(task, batches, expected_batch_count=2)

        assert result.outcome == "committed"
        assert result.committed is True
        assert result.optimizer_state_dirty is True
        assert result.processed_batch_count == 2
        assert len(observations) == 2
        assert result.sample_count == sum(
            int(batch.target.shape[0]) for batch in batches
        )
        assert result.elapsed_seconds >= 0.0
        assert _parameters_changed(task, before)

    def test_gradients_are_cleared_before_the_group(self):
        task = _synthetic_task()
        # 前の group の勾配が残っていても結果が変わらないことを確かめる
        task.training_step(_batches(1)[0]).loss.backward()

        result, _ = _run_group(task, _batches(2), expected_batch_count=2)

        assert result.outcome == "committed"


class TestOptimizerGroupAbort:
    """Commit できない group はパラメータも observation も残さない."""

    def test_fewer_batches_than_expected_are_incomplete(self):
        task = _synthetic_task()
        before = _parameters_snapshot(task)

        result, observations = _run_group(task, _batches(1), expected_batch_count=2)

        assert result.outcome == "incomplete_batches"
        assert result.committed is False
        assert result.optimizer_state_dirty is False
        assert result.processed_batch_count == 1
        assert observations == ()
        assert not _parameters_changed(task, before)

    def test_veto_discards_the_group(self):
        task = _synthetic_task()
        before = _parameters_snapshot(task)

        result, observations = _run_group(
            task, _batches(2), expected_batch_count=2, can_commit=lambda: False
        )

        assert result.outcome == "vetoed"
        assert result.optimizer_state_dirty is False
        assert observations == ()
        assert not _parameters_changed(task, before)

    def test_non_finite_loss_aborts_the_group(self):
        task = _synthetic_task(non_finite_at_step=0)
        before = _parameters_snapshot(task)

        result, observations = _run_group(task, _batches(2), expected_batch_count=2)

        assert result.outcome == "non_finite"
        assert result.optimizer_state_dirty is False
        assert observations == ()
        assert not _parameters_changed(task, before)

    def test_non_finite_gradient_without_amp_aborts_the_group(self):
        task = _InfiniteGradientTask()
        before = _parameters_snapshot(task)

        result, _ = _run_group(
            task, [torch.ones(2, 1)], expected_batch_count=1, amp_enabled=False
        )

        assert result.outcome == "non_finite"
        assert result.optimizer_state_dirty is False
        assert not _parameters_changed(task, before)

    def test_gradient_overflow_with_amp_is_not_a_failure(self):
        task = _InfiniteGradientTask()
        before = _parameters_snapshot(task)

        result, _ = _run_group(
            task, [torch.ones(2, 1)], expected_batch_count=1, amp_enabled=True
        )

        # GradScaler の step skip は AMP の正常動作なので non_finite と区別する
        assert result.outcome == "gradient_overflow"
        assert result.committed is False
        assert result.optimizer_state_dirty is True
        assert not _parameters_changed(task, before)


class TestOptimizerGroupRejections:
    """設定と task の契約違反は例外で止める."""

    def test_non_positive_expected_batch_count_is_rejected(self):
        task = _synthetic_task()

        with pytest.raises(ValueError) as exception:
            _run_group(task, _batches(1), expected_batch_count=0)

        assert "expected_batch_count" in str(exception.value)

    def test_non_positive_gradient_clip_norm_is_rejected(self):
        task = _synthetic_task()

        with pytest.raises(ValueError) as exception:
            _run_group(
                task, _batches(1), expected_batch_count=1, gradient_clip_norm=0.0
            )

        assert "gradient_clip_norm" in str(exception.value)

    def test_invalid_step_result_is_rejected(self):
        task = _InvalidStepResultTask(build_synthetic_model(seed=5))

        with pytest.raises(ValueError) as exception:
            _run_group(task, _batches(1), expected_batch_count=1)

        assert "sample_count" in str(exception.value)


class TestEvaluationBatches:
    """評価は勾配を作らず、打ち切りは例外ではなく理由で返す."""

    def test_all_batches_are_evaluated(self):
        task = _synthetic_task()
        batches = _batches(2)

        observations, reason = execute_evaluation_batches(batches, task=task)

        assert reason is None
        assert len(observations) == 2
        assert all(observation.mean.grad_fn is None for observation in observations)

    def test_stop_request_returns_a_reason(self):
        task = _synthetic_task()

        observations, reason = execute_evaluation_batches(
            _batches(2), task=task, stop_requested=lambda: True
        )

        assert reason is not None
        assert len(observations) < 2

    def test_reached_deadline_returns_a_reason(self):
        task = _synthetic_task()

        observations, reason = execute_evaluation_batches(
            _batches(2),
            task=task,
            deadline_monotonic=time.monotonic() - 1.0,
        )

        assert reason is not None
        assert len(observations) < 2

    def test_future_deadline_does_not_interrupt(self):
        task = _synthetic_task()

        observations, reason = execute_evaluation_batches(
            _batches(2),
            task=task,
            deadline_monotonic=time.monotonic() + 300.0,
        )

        assert reason is None
        assert len(observations) == 2
