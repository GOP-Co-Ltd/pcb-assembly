"""Gradient accumulation group を全成功時だけ commit する実行単位.

group の途中で停止条件が立つと、蓄積した勾配は捨てて optimizer へ触らない。

観測値も commit 後にしか返さない。

呼び出し側が乱数状態と batch 位置を group 先頭へ戻せば、その group を丸ごと
やり直せるようにするため。

AMP 有効時に GradScaler が step を skip したのは正常動作であり、失敗ではない。

``gradient_overflow`` として区別し、scale を下げたうえで同じ group を
再試行できるようにする。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Literal

import attrs
import torch
from torch.optim import Optimizer

from ml.training.task import TrainingTask

type OptimizerGroupOutcome = Literal[
    "committed",
    "incomplete_batches",
    "vetoed",
    "gradient_overflow",
    "non_finite",
]


@attrs.frozen
class OptimizerGroupResult:
    """1 group の実行結果.

    ``optimizer_state_dirty`` は optimizer と GradScaler の内部状態が変化したかを
    示す。

    ``committed`` と ``gradient_overflow`` で真になる。

    GradScaler は step を skip したときも scale を下げるため、
    やり直しても同じ状態からは始まらない。
    """

    outcome: OptimizerGroupOutcome
    optimizer_state_dirty: bool
    processed_batch_count: int
    sample_count: int
    elapsed_seconds: float

    @property
    def committed(self) -> bool:
        """Optimizer step まで到達したかどうか."""

        return self.outcome == "committed"


def execute_optimizer_group[BatchT, ObservationT](
    batches: Iterable[BatchT],
    *,
    expected_batch_count: int,
    task: TrainingTask[BatchT, ObservationT],
    optimizer: Optimizer,
    gradient_scaler: torch.GradScaler,
    gradient_clip_norm: float,
    device_type: str,
    autocast_enabled: bool,
    can_commit: Callable[[], bool] | None = None,
) -> tuple[OptimizerGroupResult, tuple[ObservationT, ...]]:
    """予定数の batch がすべて成功したときだけ optimizer へ commit する.

    観測値は commit したときだけ返す。

    捨てた group の観測値が混ざると、再試行で同じ sample を二重に数えてしまうため。
    """

    if expected_batch_count < 1:
        raise ValueError(
            f"expected_batch_count は正の整数が必要です: {expected_batch_count}"
        )
    if gradient_clip_norm <= 0:
        raise ValueError(f"gradient_clip_norm は正の値が必要です: {gradient_clip_norm}")

    started_monotonic = time.monotonic()
    observations: list[ObservationT] = []
    sample_count = 0
    optimizer.zero_grad(set_to_none=True)

    def result(
        outcome: OptimizerGroupOutcome, *, optimizer_state_dirty: bool
    ) -> OptimizerGroupResult:
        return OptimizerGroupResult(
            outcome=outcome,
            optimizer_state_dirty=optimizer_state_dirty,
            processed_batch_count=len(observations),
            sample_count=sample_count,
            elapsed_seconds=time.monotonic() - started_monotonic,
        )

    for batch in batches:
        if len(observations) >= expected_batch_count:
            raise ValueError(
                f"group が予定より多い batch を返しました: {expected_batch_count} 個"
            )
        with torch.autocast(device_type=device_type, enabled=autocast_enabled):
            step = task.training_step(batch)
        if error := step.validate():
            raise ValueError(error)
        if not bool(torch.isfinite(step.loss).item()):
            optimizer.zero_grad(set_to_none=True)
            return result("non_finite", optimizer_state_dirty=False), ()
        gradient_scaler.scale(step.loss / expected_batch_count).backward()
        observations.append(step.observation)
        sample_count += step.sample_count

    if len(observations) != expected_batch_count:
        optimizer.zero_grad(set_to_none=True)
        return result("incomplete_batches", optimizer_state_dirty=False), ()
    if can_commit is not None and not can_commit():
        optimizer.zero_grad(set_to_none=True)
        return result("vetoed", optimizer_state_dirty=False), ()

    gradient_scaler.unscale_(optimizer)
    if not _gradients_are_finite(task):
        if not gradient_scaler.is_enabled():
            optimizer.zero_grad(set_to_none=True)
            return result("non_finite", optimizer_state_dirty=False), ()
        # GradScaler は unscale_ で見つけた非有限勾配を step で skip し、update で
        # scale を下げる。同じ group をやり直すには両方を呼び切る必要がある。
        gradient_scaler.step(optimizer)
        gradient_scaler.update()
        optimizer.zero_grad(set_to_none=True)
        return result("gradient_overflow", optimizer_state_dirty=True), ()

    torch.nn.utils.clip_grad_norm_(task.model.parameters(), gradient_clip_norm)
    gradient_scaler.step(optimizer)
    gradient_scaler.update()
    return result("committed", optimizer_state_dirty=True), tuple(observations)


def execute_evaluation_batches[BatchT, ObservationT](
    batches: Iterable[BatchT],
    *,
    task: TrainingTask[BatchT, ObservationT],
    deadline_monotonic: float | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> tuple[tuple[ObservationT, ...], str | None]:
    """評価 batch を順に処理し、途中で止めたら理由文字列を添えて返す.

    停止したときの部分結果は集計に使えない。

    呼び出し側は理由が返ったら観測値を捨てること。
    """

    observations: list[ObservationT] = []
    with torch.inference_mode():
        for batch in batches:
            if reason := _evaluation_stop_reason(deadline_monotonic, stop_requested):
                return tuple(observations), reason
            observations.append(task.evaluation_step(batch))
    if reason := _evaluation_stop_reason(deadline_monotonic, stop_requested):
        return tuple(observations), reason
    return tuple(observations), None


def _evaluation_stop_reason(
    deadline_monotonic: float | None, stop_requested: Callable[[], bool] | None
) -> str | None:
    if stop_requested is not None and stop_requested():
        return "呼び出し側から評価の停止を要求されました"
    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
        return "評価の途中で deadline に到達しました"
    return None


def _gradients_are_finite[BatchT, ObservationT](
    task: TrainingTask[BatchT, ObservationT],
) -> bool:
    return all(
        parameter.grad is None or bool(torch.isfinite(parameter.grad).all().item())
        for parameter in task.model.parameters()
    )


__all__ = [
    "OptimizerGroupOutcome",
    "OptimizerGroupResult",
    "execute_evaluation_batches",
    "execute_optimizer_group",
]
