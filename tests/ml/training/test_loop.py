"""Generic optimizer-group transaction contract tests."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, override

import pytest
import torch
from torch import Tensor, nn
from torch.optim import Optimizer

from ml.training.loop import (
    OptimizerBoundary,
    OptimizerGroupResult,
    TrainingStepResult,
    execute_optimizer_group,
)


class _SquaredLossStep:
    def __init__(self, model: nn.Module) -> None:
        self._model = model

    def __call__(self, batch: Tensor) -> TrainingStepResult[float]:
        prediction = self._model(batch)
        return TrainingStepResult(
            loss=prediction.square().mean(),
            observation=float(batch.item()),
            sample_count=len(batch),
        )


class _LinearLossStep:
    def __init__(self, model: nn.Module) -> None:
        self._model = model

    def __call__(self, batch: Tensor) -> TrainingStepResult[float]:
        return TrainingStepResult(
            loss=self._model(batch).sum(),
            observation=float(batch[0, 0].item()),
            sample_count=len(batch),
        )


class _RecordingReducer:
    def __init__(self, boundary: OptimizerBoundary, *, fail: bool = False) -> None:
        self._boundary = boundary
        self._fail = fail
        self._observations: list[float] = []
        self._boundary_states: list[bool] = []

    @property
    def observations(self) -> tuple[float, ...]:
        return tuple(self._observations)

    @property
    def boundary_states(self) -> tuple[bool, ...]:
        return tuple(self._boundary_states)

    def update(self, observations: Sequence[float]) -> None:
        self._boundary_states.append(self._boundary.in_progress)
        self._observations.extend(observations)
        if self._fail:
            raise RuntimeError("metric reducer failed")


class _UpdateFailureScaler(torch.GradScaler):
    @override
    def update(self, new_scale: float | Tensor | None = None) -> None:
        super().update(new_scale)
        raise RuntimeError("scaler update failed")


def _model_and_optimizer() -> tuple[nn.Linear, torch.optim.SGD]:
    model = nn.Linear(1, 1, bias=False)
    with torch.no_grad():
        model.weight.fill_(1.0)
    return model, torch.optim.SGD(model.parameters(), lr=0.1)


def _execute_group(
    *,
    model: nn.Module,
    optimizer: Optimizer,
    scaler: torch.GradScaler,
    reducer: _RecordingReducer,
    boundary: OptimizerBoundary,
) -> OptimizerGroupResult:
    return execute_optimizer_group(
        (torch.tensor([[2.0]]),),
        expected_batch_count=1,
        model=model,
        optimizer=optimizer,
        scaler=scaler,
        gradient_clip_norm=100.0,
        device_type="cpu",
        amp_enabled=False,
        step=_SquaredLossStep(model),
        metric_reducer=reducer,
        boundary=boundary,
    )


def _raise_step_failure(
    _optimizer: Optimizer,
    _args: tuple[Any, ...],
    _kwargs: dict[str, Any],
) -> None:
    raise RuntimeError("scaler step failed")


class TestExecuteOptimizerGroupBoundary:
    def test_success_finishes_boundary_after_recording_metrics(self):
        model, optimizer = _model_and_optimizer()
        boundary = OptimizerBoundary()
        reducer = _RecordingReducer(boundary)

        result = _execute_group(
            model=model,
            optimizer=optimizer,
            scaler=torch.GradScaler("cpu"),
            reducer=reducer,
            boundary=boundary,
        )

        assert result.committed is True
        assert reducer.observations == (2.0,)
        assert reducer.boundary_states == (True,)
        assert boundary.in_progress is False

    def test_nonfinite_total_gradient_norm_rejects_commit(self):
        model = nn.Linear(2, 1, bias=False)
        with torch.no_grad():
            model.weight.fill_(1.0)
        optimizer = torch.optim.SGD(model.parameters(), lr=0.1)
        boundary = OptimizerBoundary()
        reducer = _RecordingReducer(boundary)
        batch = torch.tensor([[1e20, 1e20]])
        original_weight = model.weight.detach().clone()

        assert torch.isfinite(batch).all()
        assert torch.isinf(torch.linalg.vector_norm(batch))
        with pytest.raises(RuntimeError, match="non-finite"):
            execute_optimizer_group(
                (batch,),
                expected_batch_count=1,
                model=model,
                optimizer=optimizer,
                scaler=torch.GradScaler("cpu"),
                gradient_clip_norm=100.0,
                device_type="cpu",
                amp_enabled=False,
                step=_LinearLossStep(model),
                metric_reducer=reducer,
                boundary=boundary,
            )

        assert torch.equal(model.weight, original_weight)
        assert model.weight.grad is None
        assert reducer.observations == ()
        assert boundary.in_progress is False

    def test_step_failure_keeps_boundary_open_and_clears_gradients(self):
        model, optimizer = _model_and_optimizer()
        optimizer.register_step_post_hook(_raise_step_failure)
        boundary = OptimizerBoundary()
        reducer = _RecordingReducer(boundary)

        with pytest.raises(RuntimeError, match="scaler step failed"):
            _execute_group(
                model=model,
                optimizer=optimizer,
                scaler=torch.GradScaler("cpu"),
                reducer=reducer,
                boundary=boundary,
            )

        assert boundary.in_progress is True
        assert model.weight.grad is None
        assert reducer.observations == ()
        assert reducer.boundary_states == ()

    def test_scaler_update_failure_keeps_boundary_open_and_clears_gradients(self):
        model, optimizer = _model_and_optimizer()
        boundary = OptimizerBoundary()
        reducer = _RecordingReducer(boundary)

        with pytest.raises(RuntimeError, match="scaler update failed"):
            _execute_group(
                model=model,
                optimizer=optimizer,
                scaler=_UpdateFailureScaler("cpu"),
                reducer=reducer,
                boundary=boundary,
            )

        assert boundary.in_progress is True
        assert model.weight.grad is None
        assert reducer.observations == ()
        assert reducer.boundary_states == ()

    def test_reducer_failure_keeps_boundary_open_and_clears_gradients(self):
        model, optimizer = _model_and_optimizer()
        boundary = OptimizerBoundary()
        reducer = _RecordingReducer(boundary, fail=True)

        with pytest.raises(RuntimeError, match="metric reducer failed"):
            _execute_group(
                model=model,
                optimizer=optimizer,
                scaler=torch.GradScaler("cpu"),
                reducer=reducer,
                boundary=boundary,
            )

        assert boundary.in_progress is True
        assert model.weight.grad is None
        assert reducer.observations == (2.0,)
        assert reducer.boundary_states == (True,)
