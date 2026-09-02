"""Task非依存のpure PyTorch batch実行engine."""

from __future__ import annotations

import signal
import time
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar, override

import torch
from torch import Tensor, nn
from torch.optim import Optimizer

BatchT = TypeVar("BatchT")
ObservationT = TypeVar("ObservationT")
_BatchT_contra = TypeVar("_BatchT_contra", contravariant=True)
_ObservationT_contra = TypeVar("_ObservationT_contra", contravariant=True)
_ObservationT_co = TypeVar("_ObservationT_co", covariant=True)


@dataclass(frozen=True)
class TrainingStepResult(Generic[_ObservationT_co]):
    """1 batchのlossと、commit後にmetricへ反映する観測値."""

    loss: Tensor
    observation: _ObservationT_co
    sample_count: int

    def __post_init__(self) -> None:
        if self.loss.numel() != 1:
            raise ValueError("training step loss must be scalar")
        if self.sample_count < 1:
            raise ValueError("training step sample_count must be positive")


class TrainingStep(Protocol[_BatchT_contra, _ObservationT_co]):
    """Domain batchからdifferentiable lossとmetric観測値を作る契約."""

    def __call__(
        self, batch: _BatchT_contra
    ) -> TrainingStepResult[_ObservationT_co]: ...


class MetricReducer(Protocol[_ObservationT_contra]):
    """commit済みbatchの観測値だけを集計する契約."""

    def update(self, observations: Sequence[_ObservationT_contra]) -> None: ...


class EvaluationStep(Protocol[_BatchT_contra, _ObservationT_co]):
    """Domain batchを副作用なしの評価観測値へ変換する契約."""

    def __call__(self, batch: _BatchT_contra) -> _ObservationT_co: ...


@dataclass
class OptimizerBoundary:
    """Checkpoint rollback側へgroup commitが未完了かを公開する共有状態."""

    in_progress: bool = False


@dataclass(frozen=True)
class OptimizerGroupResult:
    """Gradient accumulation groupのcommit結果."""

    committed: bool
    processed_batch_count: int
    sample_count: int
    elapsed_seconds: float


class EvaluationStopRequested(RuntimeError):
    """評価batch境界で呼び出し側から停止を要求された."""


class TerminationSignals(AbstractContextManager["TerminationSignals"]):
    """SIGINT/SIGTERMをbatch境界で処理するための状態へ変換する."""

    def __init__(self) -> None:
        self.requested = False
        self._previous: dict[signal.Signals, Any] = {}

    @override
    def __enter__(self) -> TerminationSignals:
        if not hasattr(signal, "SIGTERM"):
            return self
        for current in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous[current] = signal.getsignal(current)
                signal.signal(current, self._handle)
            except ValueError:  # non-main thread
                self._previous.clear()
                break
        return self

    def _handle(self, _signum: int, _frame: object) -> None:
        self.requested = True

    @override
    def __exit__(self, *exc_info: object) -> None:
        for current, previous in self._previous.items():
            signal.signal(current, previous)


def gradients_are_finite(model: nn.Module) -> bool:
    """Trainable parameterに存在する全gradientがfiniteか返す."""

    return all(
        parameter.grad is None or torch.all(torch.isfinite(parameter.grad)).item()
        for parameter in model.parameters()
    )


def execute_optimizer_group(
    batches: Iterable[BatchT],
    *,
    expected_batch_count: int,
    model: nn.Module,
    optimizer: Optimizer,
    scaler: torch.GradScaler,
    gradient_clip_norm: float,
    device_type: str,
    amp_enabled: bool,
    step: TrainingStep[BatchT, ObservationT],
    metric_reducer: MetricReducer[ObservationT],
    can_commit: Callable[[], bool] | None = None,
    boundary: OptimizerBoundary | None = None,
) -> OptimizerGroupResult:
    """1 accumulation groupを全成功時だけoptimizerへcommitする.

    ``batches`` が予定数に達しない、または ``can_commit`` が偽ならgradientを
    破棄する。metric reducerもcommit後にだけ更新するため、呼び出し側は乱数状態と
    sampler位置をgroup先頭へ戻せば完全に再試行できる。
    """

    if expected_batch_count < 1:
        raise ValueError("expected_batch_count must be positive")
    if gradient_clip_norm <= 0:
        raise ValueError("gradient_clip_norm must be positive")
    started_at = time.monotonic()
    observations: list[ObservationT] = []
    sample_count = 0
    optimizer.zero_grad(set_to_none=True)
    try:
        for batch in batches:
            if len(observations) >= expected_batch_count:
                raise ValueError("optimizer group yielded too many batches")
            with torch.autocast(device_type=device_type, enabled=amp_enabled):
                result = step(batch)
                scaled_loss = result.loss / expected_batch_count
            if not torch.isfinite(result.loss).item():
                raise FloatingPointError(
                    "non-finite loss or gradient; accumulation group rolled back"
                )
            scaler.scale(scaled_loss).backward()
            observations.append(result.observation)
            sample_count += result.sample_count
        if len(observations) != expected_batch_count or (
            can_commit is not None and not can_commit()
        ):
            optimizer.zero_grad(set_to_none=True)
            return OptimizerGroupResult(
                committed=False,
                processed_batch_count=len(observations),
                sample_count=sample_count,
                elapsed_seconds=time.monotonic() - started_at,
            )
        scaler.unscale_(optimizer)
        if not gradients_are_finite(model):
            raise FloatingPointError(
                "non-finite loss or gradient; accumulation group rolled back"
            )
        torch.nn.utils.clip_grad_norm_(
            model.parameters(),
            gradient_clip_norm,
            error_if_nonfinite=True,
        )
        if boundary is not None:
            boundary.in_progress = True
        scaler.step(optimizer)
        scaler.update()
        metric_reducer.update(observations)
        if boundary is not None:
            boundary.in_progress = False
        return OptimizerGroupResult(
            committed=True,
            processed_batch_count=len(observations),
            sample_count=sample_count,
            elapsed_seconds=time.monotonic() - started_at,
        )
    except Exception:
        optimizer.zero_grad(set_to_none=True)
        raise


def execute_evaluation_batches(
    batch_factories: Iterable[Callable[[], BatchT]],
    *,
    step: EvaluationStep[BatchT, ObservationT],
    deadline_monotonic: float | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> tuple[ObservationT, ...]:
    """停止条件をbatch materialize前後に確認して評価観測値を集める."""

    def ensure_running() -> None:
        if stop_requested is not None and stop_requested():
            raise EvaluationStopRequested("evaluation stop requested")
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise TimeoutError("training finalization deadline exceeded")

    observations: list[ObservationT] = []
    with torch.inference_mode():
        for make_batch in batch_factories:
            ensure_running()
            batch = make_batch()
            ensure_running()
            observations.append(step(batch))
            ensure_running()
    return tuple(observations)


__all__ = [
    "EvaluationStep",
    "EvaluationStopRequested",
    "MetricReducer",
    "OptimizerBoundary",
    "OptimizerGroupResult",
    "TrainingStep",
    "TrainingStepResult",
    "TerminationSignals",
    "execute_evaluation_batches",
    "execute_optimizer_group",
    "gradients_are_finite",
]
