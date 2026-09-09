"""Epoch ループと、その停止条件・checkpoint・記録.

Trainer は task と data の ABC しか知らない。

epoch の進行、early stopping、learning rate scheduler、deadline、signal、
checkpoint の保存と復元だけを受け持つ。

停止は例外ではなく :class:`TrainingOutcome` の ``stop_reason`` で返す。

deadline も signal も、設定された正常な停止条件だから。

失敗として扱うのは、再試行しても直らない非有限 loss だけとする。

Optimizer state を信用できなくなった経路では ``latest.pt`` を触らない。

既知の良い再開点を、壊れた状態で上書きしないため。
"""

from __future__ import annotations

import signal as signal_module
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Literal, override

import attrs
import torch
from torch import nn
from torch.optim import Optimizer
from torch.optim.lr_scheduler import ReduceLROnPlateau

from ml.artifact.fingerprint import fingerprint_json
from ml.evaluation.compile_parity import CompileOptions
from ml.experiment.logger import ExperimentLogger, RunStatus, Scalar
from ml.training.checkpoint import (
    BestSelection,
    CheckpointRole,
    CheckpointStore,
    TrainingCheckpoint,
    TrainingProgress,
)
from ml.training.data import TrainingData
from ml.training.random_state import RandomState, seed_everything
from ml.training.task import TrainingTask
from ml.training.transaction import (
    execute_evaluation_batches,
    execute_optimizer_group,
)

type StopReason = Literal[
    "max_epochs", "max_steps", "early_stopping", "deadline", "signal"
]

# GradScaler の overflow は正常動作だが、際限なく続くなら発散している。
_MAXIMUM_CONSECUTIVE_GRADIENT_OVERFLOWS = 8

# Fingerprint から外す、その run 限りの時間予算。
#
# 学習の意味論を変えないうえ、含めると deadline で中断した run を新しい予算で
# 再開できなくなる。
_FINGERPRINT_EXCLUDED_FIELDS = (
    "deadline_seconds",
    "finalization_grace_seconds",
    "checkpoint_interval_steps",
    "checkpoint_interval_seconds",
)

_FAILURE_MESSAGE_LIMIT = 500


class NonFiniteLossError(RuntimeError):
    """Loss か gradient が非有限になり、学習を続けられない."""


class TerminationSignals(AbstractContextManager["TerminationSignals"]):
    """SIGINT / SIGTERM を flag へ落とし、group 境界で処理させる.

    Handler の中では何もせず、学習側が安全な境界で :attr:`requested` を見る。

    main thread 以外では handler を張れないので、:attr:`requested` は常に偽になる。
    """

    def __init__(self) -> None:
        self._requested = False
        self._previous: dict[int, Any] = {}

    @property
    def requested(self) -> bool:
        """停止 signal を受け取ったかどうか."""

        return self._requested

    @override
    def __enter__(self) -> TerminationSignals:
        """SIGINT と SIGTERM の handler を差し替える."""

        for number in (signal_module.SIGINT, signal_module.SIGTERM):
            try:
                previous = signal_module.getsignal(number)
                signal_module.signal(number, self._handle)
            except ValueError:
                # main thread 以外。handler を張らず requested は偽のままにする。
                self._restore()
                break
            self._previous[number] = previous
        return self

    @override
    def __exit__(self, *exc_info: object) -> None:
        """差し替えた handler を元へ戻す."""

        self._restore()

    def _handle(self, _number: int, _frame: object) -> None:
        self._requested = True

    def _restore(self) -> None:
        for number, previous in self._previous.items():
            signal_module.signal(number, previous)
        self._previous.clear()


@attrs.frozen
class TrainerConfig:
    """1 回の学習 run を決める設定.

    ``monitor`` は validation の集計キーで、``validation/`` のような接頭辞は付けない。

    early stopping と learning rate scheduler はこの 1 本を共有して見る。
    """

    max_epochs: int
    monitor: str
    mode: Literal["min", "max"] = "min"
    max_steps: int | None = None
    learning_rate: float = 1e-3
    weight_decay: float = 1e-4
    gradient_accumulation: int = 1
    gradient_clip_norm: float = 1.0
    early_stopping_patience: int = 15
    early_stopping_minimum_delta: float = 1e-4
    scheduler_factor: float = 0.5
    scheduler_patience: int = 5
    automatic_mixed_precision_enabled: bool = False
    compile_enabled: bool = False
    compile_options: CompileOptions = CompileOptions()
    deadline_seconds: float | None = None
    finalization_grace_seconds: float = 300.0
    checkpoint_interval_steps: int = 100
    checkpoint_interval_seconds: float = 600.0
    deterministic: bool = True
    seed: int = 0

    def validate(self) -> str | None:
        """設定の整合を検証する."""

        if self.max_epochs < 1:
            return f"max_epochs は正の整数が必要です: {self.max_epochs}"
        if not self.monitor:
            return "monitor は空にできません"
        if self.mode not in ("min", "max"):
            return f"mode は 'min' か 'max' が必要です: {self.mode!r}"
        if self.max_steps is not None and self.max_steps < 1:
            return f"max_steps は正の整数が必要です: {self.max_steps}"
        if self.gradient_accumulation < 1:
            return (
                "gradient_accumulation は正の整数が必要です: "
                f"{self.gradient_accumulation}"
            )
        if self.gradient_clip_norm <= 0:
            return f"gradient_clip_norm は正の値が必要です: {self.gradient_clip_norm}"
        if self.early_stopping_patience < 0:
            return (
                "early_stopping_patience は 0 以上が必要です: "
                f"{self.early_stopping_patience}"
            )
        if self.early_stopping_minimum_delta < 0:
            return (
                "early_stopping_minimum_delta は 0 以上が必要です: "
                f"{self.early_stopping_minimum_delta}"
            )
        if self.deadline_seconds is not None and self.deadline_seconds <= 0:
            return f"deadline_seconds は正の値が必要です: {self.deadline_seconds}"
        if self.finalization_grace_seconds < 0:
            return (
                "finalization_grace_seconds は 0 以上が必要です: "
                f"{self.finalization_grace_seconds}"
            )
        if error := self.compile_options.validate():
            return f"compile_options: {error}"
        return None

    @property
    def fingerprint(self) -> str:
        """Resume 互換性の判定に使う設定 fingerprint.

        その run 限りの時間予算は含めない（:data:`_FINGERPRINT_EXCLUDED_FIELDS`）。
        """

        fields = attrs.asdict(self)
        for name in _FINGERPRINT_EXCLUDED_FIELDS:
            del fields[name]
        return fingerprint_json(fields)

    def as_params(self) -> dict[str, Scalar]:
        """実験記録へ載せる、run を通して不変な params を返す.

        その run 限りの時間予算は含めない（:meth:`as_tags` へ回す）。

        param は一度記録すると値を変えられないので、resume のたびに変わりうる値を
        載せると再開そのものが失敗する。
        """

        params: dict[str, Scalar] = {}
        for field in attrs.fields(type(self)):
            if field.name in _FINGERPRINT_EXCLUDED_FIELDS:
                continue
            value = getattr(self, field.name)
            if isinstance(value, CompileOptions):
                for option in attrs.fields(CompileOptions):
                    params[f"{field.name}.{option.name}"] = _as_scalar(
                        getattr(value, option.name)
                    )
                continue
            params[field.name] = _as_scalar(value)
        return params

    def as_tags(self) -> dict[str, str]:
        """Resume のたびに変わりうる時間予算を tag として返す.

        tag は上書きできるので、run を再開して予算を変えても記録が矛盾しない。

        他の tag と同じく ``training.`` を前置きして名前空間を分ける。
        """

        return {
            f"training.{name}": str(getattr(self, name))
            for name in _FINGERPRINT_EXCLUDED_FIELDS
        }


@attrs.frozen
class TrainingOutcome:
    """1 回の学習 run が到達した地点."""

    stop_reason: StopReason
    epochs_completed: int
    global_step: int
    best_monitor_value: float | None
    best_epoch: int | None
    best_checkpoint_path: Path | None
    final_checkpoint_path: Path | None
    last_validation_metrics: Mapping[str, float]
    elapsed_seconds: float


@attrs.define(eq=False)
class _RunState:
    """1 回の ``Trainer.run`` が持ち回る、その run に固定された文脈と可変状態.

    checkpoint と失敗時の緊急保存が同じ状態を読むので、1 箇所へ集める。
    """

    signals: TerminationSignals
    optimizer: Optimizer
    scheduler: ReduceLROnPlateau
    gradient_scaler: torch.GradScaler
    run_id: str
    dataset_fingerprint: str
    config_fingerprint: str
    deadline_monotonic: float | None
    finalization_deadline: float | None
    progress: TrainingProgress
    selection: BestSelection
    validation_metrics: dict[str, float]
    best_checkpoint_path: Path | None
    stop_reason: StopReason = "max_epochs"
    final_checkpoint_path: Path | None = None
    last_checkpoint_step: int = 0
    last_checkpoint_monotonic: float = 0.0


@attrs.frozen(eq=False)
class _EpochTraining[ObservationT]:
    """1 epoch の学習部分が集めた観測値と、途中で立った停止条件."""

    observations: tuple[ObservationT, ...]
    sample_count: int
    stop_reason: StopReason | None


class Trainer[BatchT, ObservationT]:
    """Task と data を受け取り、epoch ループを回して checkpoint を残す.

    Device は ``__init__`` の keyword で受け取り、設定 fingerprint に含めない。

    同じ設定の run を CPU と GPU のあいだで再開できるようにするため。
    """

    def __init__(
        self,
        task: TrainingTask[BatchT, ObservationT],
        data: TrainingData[BatchT],
        *,
        config: TrainerConfig,
        store: CheckpointStore,
        logger: ExperimentLogger,
        device: torch.device | None = None,
    ) -> None:
        if error := config.validate():
            raise ValueError(error)
        self._task = task
        self._data = data
        self._config = config
        self._store = store
        self._logger = logger
        self._device = device if device is not None else _default_device()
        self._run_ended = False

    @property
    def device(self) -> torch.device:
        """Model と batch を置く device."""

        return self._device

    def run(
        self,
        *,
        resume_from: Path | None = None,
        run_kind: str = "training",
        run_name: str | None = None,
    ) -> TrainingOutcome:
        """学習を最後まで回し、到達した地点を返す.

        ``resume_from`` を渡すと、その checkpoint の続きから再開する。
        """

        config = self._config
        started_monotonic = time.monotonic()
        self._run_ended = False
        seed_everything(config.seed, deterministic=config.deterministic)

        model = self._task.model
        model.to(self._device)
        optimizer = torch.optim.AdamW(
            [parameter for parameter in model.parameters() if parameter.requires_grad],
            lr=config.learning_rate,
            weight_decay=config.weight_decay,
        )
        scheduler = ReduceLROnPlateau(
            optimizer,
            mode=config.mode,
            factor=config.scheduler_factor,
            patience=config.scheduler_patience,
        )
        gradient_scaler = torch.GradScaler(
            self._device.type, enabled=config.automatic_mixed_precision_enabled
        )

        dataset_fingerprint = self._data.dataset_fingerprint
        config_fingerprint = config.fingerprint
        progress = TrainingProgress()
        selection = BestSelection(
            monitor=config.monitor,
            mode=config.mode,
            minimum_delta=config.early_stopping_minimum_delta,
        )
        validation_metrics: dict[str, float] = {}
        resumed_run_id: str | None = None
        checkpoint: TrainingCheckpoint | None = None

        if resume_from is not None:
            checkpoint = self._load_resume_point(
                resume_from,
                model=model,
                dataset_fingerprint=dataset_fingerprint,
                config_fingerprint=config_fingerprint,
            )
            progress = checkpoint.progress
            selection = checkpoint.selection
            validation_metrics = dict(checkpoint.validation_metrics)
            resumed_run_id = checkpoint.run_id

        run_id = self._logger.start(run_kind=run_kind, run_name=run_name)
        if resumed_run_id is not None and run_id != resumed_run_id:
            self._end_run("FAILED")
            raise ValueError(
                "resume 元の checkpoint と logger の run_id が一致しません: "
                f"{resumed_run_id!r} と {run_id!r}"
            )
        deadline_monotonic = (
            None
            if config.deadline_seconds is None
            else started_monotonic + config.deadline_seconds
        )
        with TerminationSignals() as signals:
            state = _RunState(
                signals=signals,
                optimizer=optimizer,
                scheduler=scheduler,
                gradient_scaler=gradient_scaler,
                run_id=run_id,
                dataset_fingerprint=dataset_fingerprint,
                config_fingerprint=config_fingerprint,
                deadline_monotonic=deadline_monotonic,
                finalization_deadline=(
                    None
                    if deadline_monotonic is None
                    else deadline_monotonic + config.finalization_grace_seconds
                ),
                progress=progress,
                selection=selection,
                validation_metrics=validation_metrics,
                best_checkpoint_path=None,
                last_checkpoint_monotonic=time.monotonic(),
            )
            try:
                # start() のあとの復元と記録は必ず例外経路の内側へ置く。ここで
                # 落ちると run が RUNNING のまま残るため。
                #
                # 重みの書き換えを run_id 照合より後に回すのも兼ねる。拒否した
                # resume で呼び出し側の model を壊さない。
                if checkpoint is not None:
                    self._restore(
                        checkpoint,
                        model=model,
                        optimizer=optimizer,
                        scheduler=scheduler,
                        gradient_scaler=gradient_scaler,
                    )
                    # 新規 run では前の run の best.pt を必ず捨てるので読まない。
                    state.best_checkpoint_path = self._existing_best_path(run_id)
                self._logger.log_params(
                    {
                        **config.as_params(),
                        "config_fingerprint": config_fingerprint,
                        "dataset_fingerprint": dataset_fingerprint,
                    }
                )
                self._logger.set_tags(config.as_tags())
                if config.compile_enabled and not _deadline_passed(deadline_monotonic):
                    self._task.compile_forward(config.compile_options)
                if resume_from is None:
                    self._save(state, "latest")
                self._run_epochs(state)
                self._finalize(state)
            except BaseException as error:
                self._capture_failure(state, error)
                self._end_run("FAILED")
                raise

        self._end_run("KILLED" if state.stop_reason == "signal" else "FINISHED")
        return TrainingOutcome(
            stop_reason=state.stop_reason,
            epochs_completed=state.progress.epochs_completed,
            global_step=state.progress.global_step,
            best_monitor_value=state.selection.best_value,
            best_epoch=state.selection.best_epoch,
            best_checkpoint_path=state.best_checkpoint_path,
            final_checkpoint_path=state.final_checkpoint_path,
            last_validation_metrics=dict(state.validation_metrics),
            elapsed_seconds=time.monotonic() - started_monotonic,
        )

    def _load_resume_point(
        self,
        resume_from: Path,
        *,
        model: nn.Module,
        dataset_fingerprint: str,
        config_fingerprint: str,
    ) -> TrainingCheckpoint:
        checkpoint, reason = self._store.load_path(resume_from)
        if checkpoint is None:
            raise ValueError(reason)
        # run_id はここでは照合しない。logger をまだ start していないため、
        # 実際の run_id との一致は start 直後に確かめる。
        rejection = checkpoint.resume_rejection(
            dataset_fingerprint=dataset_fingerprint,
            config_fingerprint=config_fingerprint,
            model_state_keys=set(model.state_dict()),
        )
        if rejection is not None:
            raise ValueError(rejection)
        return checkpoint

    def _restore(
        self,
        checkpoint: TrainingCheckpoint,
        *,
        model: nn.Module,
        optimizer: Optimizer,
        scheduler: ReduceLROnPlateau,
        gradient_scaler: torch.GradScaler,
    ) -> None:
        model.load_state_dict(dict(checkpoint.model_state))
        optimizer.load_state_dict(dict(checkpoint.optimizer_state))
        scheduler.load_state_dict(dict(checkpoint.scheduler_state))
        if gradient_scaler.is_enabled():
            gradient_scaler.load_state_dict(dict(checkpoint.gradient_scaler_state))
        restored, reason = checkpoint.random_state.restore()
        if not restored:
            raise ValueError(reason)

    def _run_epochs(self, state: _RunState) -> None:
        while state.progress.epoch < self._config.max_epochs:
            if not self._run_epoch(state):
                return
        state.stop_reason = "max_epochs"

    def _run_epoch(self, state: _RunState) -> bool:
        config = self._config
        epoch_started = time.monotonic()
        plan = self._data.plan_epoch(split="train", epoch=state.progress.epoch)
        if state.progress.batch_plan:
            if state.progress.batch_plan != plan:
                raise ValueError(
                    "resume 元の batch_plan が現在の計画と一致しません: "
                    f"epoch {state.progress.epoch}"
                )
        else:
            state.progress = state.progress.with_batch_plan(plan)

        self._task.model.train()
        training = self._train_epoch(state)
        if training.stop_reason is not None:
            # 中断した epoch は validation を走らせない。部分 epoch の metric は
            # 完走した epoch と比較できないため。
            state.stop_reason = training.stop_reason
            self._save(state, "latest")
            return False

        self._task.model.eval()
        observations, reason = execute_evaluation_batches(
            self._validation_batches(state),
            task=self._task,
            deadline_monotonic=state.deadline_monotonic,
            stop_requested=lambda: state.signals.requested,
        )
        if reason is not None:
            state.stop_reason = "signal" if state.signals.requested else "deadline"
            self._save(state, "latest")
            return False

        metrics = self._task.reduce(observations)
        epoch_seconds = time.monotonic() - epoch_started
        train_metrics = self._task.reduce(training.observations)
        # 集計は monitor 検査より前に log する。monitor を欠く run こそ原因
        # （平均飽和など）の診断が要るのに、raise を先に置くと値が捨てられ、
        # run をまたいだ推移を追えなくなる。learning_rate は scheduler を進める
        # 前の値、つまりこの epoch で実際に使った値になる。
        self._logger.log_metrics(
            {
                **{f"train/{name}": value for name, value in train_metrics.items()},
                **{f"validation/{name}": value for name, value in metrics.items()},
                "learning_rate": float(state.optimizer.param_groups[0]["lr"]),
                "epoch_seconds": epoch_seconds,
                "train_samples_per_second": (
                    training.sample_count / epoch_seconds if epoch_seconds > 0 else 0.0
                ),
            },
            step=state.progress.global_step,
        )
        if config.monitor not in metrics:
            raise ValueError(
                f"validation の集計に monitor {config.monitor!r} がありません: "
                f"{_formatted_metrics(metrics)}"
            )
        monitor_value = float(metrics[config.monitor])
        state.validation_metrics = {
            name: float(value) for name, value in metrics.items()
        }
        state.scheduler.step(monitor_value)
        state.selection, improved = state.selection.consider(
            monitor_value, epoch=state.progress.epoch
        )
        if improved:
            state.best_checkpoint_path = self._save(state, "best")
        state.progress = state.progress.with_completed_epoch()
        self._save(state, "latest")
        if state.selection.patience_counter >= config.early_stopping_patience:
            state.stop_reason = "early_stopping"
            return False
        return True

    def _train_epoch(self, state: _RunState) -> _EpochTraining[ObservationT]:
        config = self._config
        plan = state.progress.batch_plan
        observations: list[ObservationT] = []
        sample_count = 0
        consecutive_overflows = 0
        while state.progress.next_batch_index < len(plan):
            if stop_reason := self._stop_reason_now(state):
                return _EpochTraining(tuple(observations), sample_count, stop_reason)
            index = state.progress.next_batch_index
            group = plan[index : index + config.gradient_accumulation]
            snapshot = RandomState.capture()
            result, group_observations = execute_optimizer_group(
                self._training_batches(state, group),
                expected_batch_count=len(group),
                task=self._task,
                optimizer=state.optimizer,
                gradient_scaler=state.gradient_scaler,
                gradient_clip_norm=config.gradient_clip_norm,
                device_type=self._device.type,
                autocast_enabled=config.automatic_mixed_precision_enabled,
                can_commit=lambda: self._stop_reason_now(state) is None,
            )
            match result.outcome:
                case "committed":
                    state.progress = state.progress.with_committed_group(
                        index + len(group)
                    )
                    observations.extend(group_observations)
                    sample_count += result.sample_count
                    consecutive_overflows = 0
                    self._save_on_interval(state)
                case "gradient_overflow":
                    self._rewind(snapshot)
                    consecutive_overflows += 1
                    if consecutive_overflows >= _MAXIMUM_CONSECUTIVE_GRADIENT_OVERFLOWS:
                        raise NonFiniteLossError(
                            "gradient overflow が "
                            f"{consecutive_overflows} 回続きました: "
                            f"epoch {state.progress.epoch}、batch {index}"
                        )
                case "incomplete_batches" | "vetoed":
                    self._rewind(snapshot)
                    stop_reason = self._stop_reason_now(state)
                    if stop_reason is None:
                        raise ValueError(
                            "停止条件がないのに group を commit できませんでした: "
                            f"{result.outcome}"
                        )
                    return _EpochTraining(
                        tuple(observations), sample_count, stop_reason
                    )
                case "non_finite":
                    raise NonFiniteLossError(
                        "loss または gradient が非有限になりました: "
                        f"epoch {state.progress.epoch}、batch {index}"
                    )
        return _EpochTraining(tuple(observations), sample_count, None)

    def _training_batches(
        self, state: _RunState, group: Sequence[tuple[str, ...]]
    ) -> Iterator[BatchT]:
        for sample_ids in group:
            yield self._data.materialize(
                sample_ids,
                split="train",
                epoch=state.progress.epoch,
                training=True,
                device=self._device,
            )

    def _validation_batches(self, state: _RunState) -> Iterator[BatchT]:
        epoch = state.progress.epoch
        for sample_ids in self._data.plan_epoch(split="validation", epoch=epoch):
            yield self._data.materialize(
                sample_ids,
                split="validation",
                epoch=epoch,
                training=False,
                device=self._device,
            )

    def _stop_reason_now(self, state: _RunState) -> StopReason | None:
        if state.signals.requested:
            return "signal"
        if _deadline_passed(state.deadline_monotonic):
            return "deadline"
        max_steps = self._config.max_steps
        if max_steps is not None and state.progress.global_step >= max_steps:
            return "max_steps"
        return None

    def _rewind(self, snapshot: RandomState) -> None:
        restored, reason = snapshot.restore()
        if not restored:
            raise ValueError(reason)

    def _save_on_interval(self, state: _RunState) -> None:
        config = self._config
        steps_since = state.progress.global_step - state.last_checkpoint_step
        seconds_since = time.monotonic() - state.last_checkpoint_monotonic
        if (
            steps_since >= config.checkpoint_interval_steps
            or seconds_since >= config.checkpoint_interval_seconds
        ):
            self._save(state, "latest")

    def _save(self, state: _RunState, role: CheckpointRole) -> Path:
        checkpoint = TrainingCheckpoint(
            role=role,
            created_unix_seconds=time.time(),
            run_id=state.run_id,
            progress=state.progress,
            selection=state.selection,
            validation_metrics=dict(state.validation_metrics),
            dataset_fingerprint=state.dataset_fingerprint,
            config_fingerprint=state.config_fingerprint,
            model_state={
                name: tensor.detach().cpu().clone()
                for name, tensor in self._task.model.state_dict().items()
            },
            optimizer_state=state.optimizer.state_dict(),
            scheduler_state=state.scheduler.state_dict(),
            gradient_scaler_state=state.gradient_scaler.state_dict(),
            random_state=RandomState.capture(),
        )
        path = self._store.save(checkpoint)
        state.last_checkpoint_step = state.progress.global_step
        state.last_checkpoint_monotonic = time.monotonic()
        return path

    def _finalize(self, state: _RunState) -> None:
        if _deadline_passed(state.finalization_deadline):
            # 猶予を使い切ったら latest.pt のまま終える。best の読み戻しと
            # final の書き出しを中途半端に始めない。
            return
        best, reason = self._best_of_this_run(state.run_id)
        if best is not None:
            self._task.model.load_state_dict(dict(best.model_state))
            state.best_checkpoint_path = self._store.path_for("best")
        elif reason is not None:
            # 別 run の best.pt を今回の final.pt として書き出さない。
            state.best_checkpoint_path = None
            self._logger.set_tags(
                {
                    "finalization.best_checkpoint_ignored": reason[
                        :_FAILURE_MESSAGE_LIMIT
                    ]
                }
            )
        state.final_checkpoint_path = self._save(state, "final")
        self._logger.log_artifact(state.final_checkpoint_path)

    def _best_of_this_run(
        self, run_id: str
    ) -> tuple[TrainingCheckpoint | None, str | None]:
        """この run が書いた ``best.pt`` だけを返す.

        checkpoint directory を使い回すと前の run の ``best.pt`` が残っている。

        別 run の重みを今回の成果として扱わないよう、``run_id`` で持ち主を確かめる。
        """

        if not self._store.exists("best"):
            return None, None
        best, reason = self._store.load("best")
        if best is None:
            return None, f"best.pt を読めませんでした: {reason}"
        if best.run_id != run_id:
            return None, (
                f"best.pt は別の run のものです: {best.run_id!r}（今回 {run_id!r}）"
            )
        return best, None

    def _existing_best_path(self, run_id: str) -> Path | None:
        best, _ = self._best_of_this_run(run_id)
        return None if best is None else self._store.path_for("best")

    def _capture_failure(self, state: _RunState, error: BaseException) -> None:
        """失敗時の live 状態を ``emergency.pt`` へ落とし、タグに理由を残す.

        ``latest.pt`` は触らない。既知の良い再開点を壊さないため。
        """

        tags = {
            "failure.type": type(error).__name__,
            "failure.message": str(error)[:_FAILURE_MESSAGE_LIMIT],
        }
        try:
            path = self._save(state, "emergency")
        except Exception as capture_error:  # noqa: BLE001 - 失敗も記録に残す
            tags["failure.emergency_checkpoint"] = "保存できませんでした"
            tags["failure.emergency_error"] = str(capture_error)[
                :_FAILURE_MESSAGE_LIMIT
            ]
        else:
            tags["failure.emergency_checkpoint"] = path.name
        try:
            self._logger.set_tags(tags)
        except Exception:  # noqa: BLE001 - logger の失敗で元の例外を隠さない
            return

    def _end_run(self, status: RunStatus) -> None:
        if self._run_ended:
            return
        self._run_ended = True
        self._logger.end(status=status)


def _default_device() -> torch.device:
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def _deadline_passed(deadline_monotonic: float | None) -> bool:
    return deadline_monotonic is not None and time.monotonic() >= deadline_monotonic


def _formatted_metrics(metrics: Mapping[str, float]) -> str:
    """集計結果を ``名前=値`` の並びへ落とす.

    monitor を欠いた失敗を自己説明的にするため、key 名だけでなく値も例外文へ出す。
    """

    return ", ".join(f"{name}={value:g}" for name, value in sorted(metrics.items()))


def _as_scalar(value: object) -> Scalar:
    if isinstance(value, bool | int | float | str):
        return value
    return str(value)


__all__ = [
    "NonFiniteLossError",
    "StopReason",
    "TerminationSignals",
    "Trainer",
    "TrainerConfig",
    "TrainingOutcome",
]
