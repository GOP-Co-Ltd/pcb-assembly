"""Framework非依存のpure PyTorch学習、評価、checkpoint core."""

from __future__ import annotations

import json
import math
import os
import re
import resource
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import NormalDist
from typing import Any, Literal, Never, cast

import torch
from torch import Tensor

from ml.evaluation.compile_parity import torch_compile_graph_break_count
from ml.model.inspection import model_parameter_count
from ml.model.regression import (
    validate_gaussian_nll_inputs as validate_loss_inputs,
    weighted_gaussian_nll,
)
from ml.training.experiment import ExperimentLogger, Scalar, write_json_artifact
from ml.training.loop import (
    EvaluationStopRequested,
    OptimizerBoundary,
    TerminationSignals as _TerminationSignals,
    TrainingStepResult,
    execute_optimizer_group,
    gradients_are_finite,
)
from ml.training.random_state import (
    capture_random_state as _rng_state,
    restore_random_state as _restore_rng_state,
    seed_everything as _seed_everything,
)

from .formal_artifact import (
    publish_formal_artifact,
)
from .metrics import (
    EvaluationPredictions as _EvaluationPredictions,
    RegressionMetricReducer as _RegressionMetricReducer,
    RegressionMetrics as _RegressionMetrics,
    TrainingObservation as _TrainingObservation,
    evaluate_batches as _evaluate_batches,
    fit_log_variance_offset as _fit_log_variance_offset,
)
from .model import (
    PasteVolumeModelConfig,
    PasteVolumeResNet,
    model_gmac,
    validate_model_inputs,
)
from .training_artifacts import (
    CHECKPOINT_KIND as _CHECKPOINT_KIND,
    CHECKPOINT_SCHEMA_VERSION as _CHECKPOINT_SCHEMA_VERSION,
    FormalTrainingWeights as _FormalTrainingWeights,
    RunKind as _RunKind,
    atomic_torch_save as _atomic_torch_save,
    extract_best_weights as _extract_best_weights,
    file_sha256 as _file_sha256,
    load_formal_training_weights as _load_formal_training_weights,
    load_model_weights as _load_model_weights,
    load_training_checkpoint as _load_training_checkpoint,
)
from .training_types import (
    TrainingBatch as _TrainingBatch,
    TrainingData as _TrainingData,
)

_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")

type StopReason = Literal[
    "max_epochs", "max_steps", "early_stopping", "deadline", "signal"
]


class TrainingInterrupted(RuntimeError):
    """signal受信後に再開可能checkpointだけを公開して学習を中断したことを示す."""

    def __init__(self, *, run_id: str, checkpoint: Path) -> None:
        super().__init__(f"training interrupted; resume from {checkpoint}")
        self.run_id = run_id
        self.checkpoint = checkpoint


class TrainingDeadlineExceeded(RuntimeError):
    """有効な学習stepを作る前にtime budgetを使い切ったことを示す."""

    def __init__(self, *, run_id: str, checkpoint: Path) -> None:
        super().__init__(f"training deadline expired; resume from {checkpoint}")
        self.run_id = run_id
        self.checkpoint = checkpoint


@dataclass(frozen=True)
class TrainerConfig:
    """学習loopを完全に決めるfrozen設定."""

    device: str = "auto"
    seed: int = 42
    learning_rate: float = 3e-4
    weight_decay: float = 1e-4
    gradient_accumulation_steps: int = 1
    gradient_clip_norm: float = 1.0
    max_epochs: int = 200
    max_steps: int | None = None
    early_stopping_patience: int = 15
    early_stopping_min_delta: float = 1e-4
    scheduler_factor: float = 0.5
    scheduler_patience: int = 5
    compile_enabled: bool = True
    compile_backend: str = "inductor"
    compile_mode: str = "default"
    deterministic: bool = True
    deadline_seconds: float | None = None
    finalization_grace_seconds: float = 300.0
    max_train_samples: int | None = None
    fine_tune_full_model: bool = False

    def __post_init__(self) -> None:
        if self.device not in ("auto", "cpu", "cuda"):
            raise ValueError("device must be auto, cpu, or cuda")
        if self.learning_rate <= 0 or self.weight_decay < 0:
            raise ValueError("optimizer values are invalid")
        positive_values = (
            self.gradient_accumulation_steps,
            self.max_epochs,
            self.early_stopping_patience,
            self.scheduler_patience,
        )
        if any(value < 1 for value in positive_values):
            raise ValueError("step, epoch, and patience values must be positive")
        if self.gradient_clip_norm <= 0:
            raise ValueError("gradient_clip_norm must be positive")
        if self.max_steps is not None and self.max_steps < 1:
            raise ValueError("max_steps must be positive when specified")
        if self.deadline_seconds is not None and self.deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be positive when specified")
        if self.finalization_grace_seconds <= 0:
            raise ValueError("finalization_grace_seconds must be positive")
        if self.max_train_samples is not None and self.max_train_samples < 1:
            raise ValueError("max_train_samples must be positive when specified")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CheckpointConfig:
    directory: Path
    resume_checkpoint: Path | None = None
    initial_weights: Path | None = None
    save_interval_steps: int = 500
    save_interval_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.save_interval_steps < 1 or self.save_interval_seconds <= 0:
            raise ValueError("checkpoint save intervals must be positive")
        if (
            self.resume_checkpoint is not None
            and self.resume_checkpoint.resolve().parent != self.directory.resolve()
        ):
            raise ValueError("resume must use the checkpoint's existing run directory")


@dataclass(frozen=True)
class TrainingCoreConfig:
    run_kind: _RunKind
    model: PasteVolumeModelConfig
    trainer: TrainerConfig
    checkpoint: CheckpointConfig
    dataset_fingerprint: str
    split_fingerprint: str
    config_fingerprint: str
    training_protocol_fingerprint: str
    preprocess_schema: Mapping[str, object]
    train_sample_ids: tuple[str, ...]
    parent_base_run_id: str | None = None
    run_tags: Mapping[str, str] = field(default_factory=dict)
    run_params: Mapping[str, Scalar] = field(default_factory=dict)
    resolved_config: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            self.run_kind == "finetune"
            and self.checkpoint.initial_weights is None
            and self.checkpoint.resume_checkpoint is None
        ):
            raise ValueError("new finetune run requires initial_weights")
        if (
            self.run_kind == "base-train"
            and self.checkpoint.initial_weights is not None
        ):
            raise ValueError("base training cannot use initial_weights")
        for name, value in (
            ("dataset_fingerprint", self.dataset_fingerprint),
            ("split_fingerprint", self.split_fingerprint),
            ("config_fingerprint", self.config_fingerprint),
        ):
            if not _SHA256_PATTERN.fullmatch(value):
                raise ValueError(f"{name} must be an exact sha256 fingerprint")
        if not _SHA256_PATTERN.fullmatch(self.training_protocol_fingerprint):
            raise ValueError(
                "training_protocol_fingerprint must be an exact sha256 fingerprint"
            )
        if not self.train_sample_ids or len(set(self.train_sample_ids)) != len(
            self.train_sample_ids
        ):
            raise ValueError("train_sample_ids must be non-empty and unique")


@dataclass(frozen=True)
class TrainResult:
    run_id: str
    latest_checkpoint: Path
    best_checkpoint: Path
    final_checkpoint: Path
    weights_path: Path
    epochs_completed: int
    global_step: int
    stopped_reason: StopReason
    best_validation_metrics: _RegressionMetrics
    calibrated_validation_metrics: _RegressionMetrics
    log_variance_offset: float
    dataset_fingerprint: str
    split_fingerprint: str
    parent_run_id: str | None
    parent_checkpoint_id: str | None


@dataclass
class _TrainState:
    epoch: int = 0
    global_step: int = 0
    next_batch_index: int = 0
    batch_plan: tuple[tuple[str, ...], ...] = ()
    best_validation_nll: float = math.inf
    best_validation_mae: float = math.inf
    best_validation_metrics: _RegressionMetrics | None = None
    best_epoch: int = -1
    patience_counter: int = 0
    epochs_completed: int = 0


def _resolve_device(value: str) -> torch.device:
    if value == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if value == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but is not available")
    return torch.device(value)


def _atomic_write_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def _write_scatter_svg(
    path: Path,
    *,
    x_values: Sequence[float],
    y_values: Sequence[float],
    title: str,
    x_label: str,
    y_label: str,
    reference: Literal["diagonal", "zero", "none"] = "none",
    connect: bool = False,
) -> Path:
    if len(x_values) != len(y_values) or not x_values:
        raise ValueError("plot values must be non-empty and have matching lengths")
    if not all(math.isfinite(value) for value in (*x_values, *y_values)):
        raise ValueError("plot values must be finite")
    width, height = 640, 400
    left, right, top, bottom = 70, 20, 35, 55
    x_min, x_max = min(x_values), max(x_values)
    y_min, y_max = min(y_values), max(y_values)
    if reference == "diagonal":
        common_min = min(x_min, y_min)
        common_max = max(x_max, y_max)
        x_min = y_min = common_min
        x_max = y_max = common_max
    elif reference == "zero":
        y_min = min(y_min, 0.0)
        y_max = max(y_max, 0.0)
    if x_min == x_max:
        x_min -= 0.5
        x_max += 0.5
    if y_min == y_max:
        y_min -= 0.5
        y_max += 0.5
    x_padding = (x_max - x_min) * 0.05
    y_padding = (y_max - y_min) * 0.05
    x_min -= x_padding
    x_max += x_padding
    y_min -= y_padding
    y_max += y_padding
    plot_width = width - left - right
    plot_height = height - top - bottom

    def point(x_value: float, y_value: float) -> tuple[float, float]:
        x = left + (x_value - x_min) / (x_max - x_min) * plot_width
        y = top + (y_max - y_value) / (y_max - y_min) * plot_height
        return x, y

    points = [
        point(x_value, y_value)
        for x_value, y_value in zip(x_values, y_values, strict=True)
    ]
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{width / 2}" y="22" text-anchor="middle" font-family="sans-serif" font-size="16">{title}</text>',
        f'<line x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" y2="{top + plot_height}" stroke="black"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" stroke="black"/>',
    ]
    if reference == "diagonal":
        x1, y1 = point(max(x_min, y_min), max(x_min, y_min))
        x2, y2 = point(min(x_max, y_max), min(x_max, y_max))
        elements.append(
            f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" stroke="#888" stroke-dasharray="5 4"/>'
        )
    elif reference == "zero":
        x1, y = point(x_min, 0.0)
        x2, _ = point(x_max, 0.0)
        elements.append(
            f'<line x1="{x1:.2f}" y1="{y:.2f}" x2="{x2:.2f}" y2="{y:.2f}" stroke="#888" stroke-dasharray="5 4"/>'
        )
    if connect:
        polyline = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
        elements.append(
            f'<polyline points="{polyline}" fill="none" stroke="#1f77b4" stroke-width="1.5"/>'
        )
    elements.extend(
        f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.5" fill="#1f77b4"/>' for x, y in points
    )
    elements.extend(
        (
            f'<text x="{width / 2}" y="{height - 12}" text-anchor="middle" font-family="sans-serif" font-size="12">{x_label}</text>',
            f'<text x="16" y="{height / 2}" text-anchor="middle" transform="rotate(-90 16 {height / 2})" font-family="sans-serif" font-size="12">{y_label}</text>',
            f'<text x="{left}" y="{height - 32}" font-family="monospace" font-size="10">{x_min:.4g}</text>',
            f'<text x="{left + plot_width}" y="{height - 32}" text-anchor="end" font-family="monospace" font-size="10">{x_max:.4g}</text>',
            f'<text x="{left - 8}" y="{top + 4}" text-anchor="end" font-family="monospace" font-size="10">{y_max:.4g}</text>',
            f'<text x="{left - 8}" y="{top + plot_height}" text-anchor="end" font-family="monospace" font-size="10">{y_min:.4g}</text>',
            "</svg>\n",
        )
    )
    return _atomic_write_text(path, "\n".join(elements))


def _checkpoint_payload(
    *,
    role: Literal["latest", "best", "final", "emergency"],
    run_id: str,
    model: PasteVolumeResNet,
    optimizer: torch.optim.Optimizer,
    scheduler: torch.optim.lr_scheduler.ReduceLROnPlateau,
    scaler: torch.GradScaler,
    state: _TrainState,
    config: TrainingCoreConfig,
    rng_state: Mapping[str, object] | None = None,
    parent_run_id: str | None,
    parent_checkpoint_id: str | None,
) -> dict[str, object]:
    return {
        "kind": _CHECKPOINT_KIND,
        "schema_version": _CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_role": role,
        "created_unix_seconds": time.time(),
        "run_id": run_id,
        "model_config": config.model.to_dict(),
        "model_state": model.state_dict(),
        "optimizer_state": optimizer.state_dict(),
        "scheduler_state": scheduler.state_dict(),
        "grad_scaler_state": scaler.state_dict(),
        "epoch": state.epoch,
        "global_step": state.global_step,
        "next_batch_index": state.next_batch_index,
        "epochs_completed": state.epochs_completed,
        "batch_plan": [list(batch) for batch in state.batch_plan],
        "best_selection_state": {
            "validation_nll": state.best_validation_nll,
            "validation_mae_ul": state.best_validation_mae,
            "epoch": state.best_epoch,
            "patience_counter": state.patience_counter,
        },
        "best_validation_metrics": (
            asdict(state.best_validation_metrics)
            if state.best_validation_metrics is not None
            else None
        ),
        "rng_state": dict(rng_state or _rng_state()),
        "dataset_fingerprint": config.dataset_fingerprint,
        "split_fingerprint": config.split_fingerprint,
        "config_fingerprint": config.config_fingerprint,
        "training_protocol_fingerprint": config.training_protocol_fingerprint,
        "resolved_config": dict(config.resolved_config),
        "preprocess_schema": dict(config.preprocess_schema),
        "uncertainty_log_variance_offset": None,
        "train_sample_ids": list(config.train_sample_ids),
        "trainer_config": config.trainer.to_dict(),
        "run_kind": config.run_kind,
        "parent_run_id": parent_run_id,
        "parent_checkpoint_id": parent_checkpoint_id,
    }


def _state_from_checkpoint(payload: Mapping[str, object]) -> _TrainState:
    selection = cast(Mapping[str, object], payload["best_selection_state"])
    raw_metrics = payload.get("best_validation_metrics")
    metrics_raw = (
        cast(Mapping[str, Any], raw_metrics) if isinstance(raw_metrics, dict) else None
    )
    metrics = (
        _RegressionMetrics(**dict(metrics_raw)) if metrics_raw is not None else None
    )
    return _TrainState(
        epoch=cast(int, payload["epoch"]),
        global_step=cast(int, payload["global_step"]),
        next_batch_index=cast(int, payload["next_batch_index"]),
        batch_plan=tuple(
            tuple(cast(Sequence[str], batch))
            for batch in cast(Sequence[Sequence[str]], payload["batch_plan"])
        ),
        best_validation_nll=float(cast(Any, selection["validation_nll"])),
        best_validation_mae=float(cast(Any, selection["validation_mae_ul"])),
        best_epoch=cast(int, selection["epoch"]),
        patience_counter=cast(int, selection["patience_counter"]),
        best_validation_metrics=metrics,
        epochs_completed=cast(int, payload["epochs_completed"]),
    )


def _validate_resume(payload: Mapping[str, object], config: TrainingCoreConfig) -> None:
    expected = {
        "dataset_fingerprint": config.dataset_fingerprint,
        "split_fingerprint": config.split_fingerprint,
        "config_fingerprint": config.config_fingerprint,
        "training_protocol_fingerprint": config.training_protocol_fingerprint,
        "resolved_config": dict(config.resolved_config),
        "model_config": config.model.to_dict(),
        "trainer_config": config.trainer.to_dict(),
        "run_kind": config.run_kind,
        "train_sample_ids": list(config.train_sample_ids),
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ValueError(f"resume {key} does not match the current run")


def _load_initial_weights(
    path: Path,
    model: PasteVolumeResNet,
    config: TrainingCoreConfig,
    *,
    formal_tracking_uri: str | None,
) -> tuple[str, str, str]:
    formal_weights: _FormalTrainingWeights | None = None
    if formal_tracking_uri is not None:
        formal_weights = _load_formal_training_weights(
            path,
            tracking_uri=formal_tracking_uri,
        )
        if formal_weights.run_kind != "base-train":
            raise ValueError(
                "formal finetune initial weights must come from a base-train run"
            )
    payload = _load_model_weights(path)
    if payload["source_checkpoint_role"] != "best":
        raise ValueError("initial weights must come from a best checkpoint")
    if payload["model_config"] != config.model.to_dict():
        raise ValueError("initial weight model config does not match")
    if payload["preprocess_schema"] != dict(config.preprocess_schema):
        raise ValueError("initial weight preprocess schema does not match")
    source_run_id = cast(str, payload["source_run_id"])
    source_checkpoint_id = cast(str, payload["source_checkpoint_sha256"])
    if formal_weights is not None and formal_weights.source_run_id != source_run_id:
        raise ValueError("formal initial weight lineage does not match")
    if config.parent_base_run_id not in (None, source_run_id):
        raise ValueError("parent_base_run_id does not match initial weight lineage")
    state_dict = cast(Mapping[str, Tensor], payload["state_dict"])
    model.load_state_dict(state_dict, strict=True)
    return source_run_id, source_checkpoint_id, _file_sha256(path)


def _save_checkpoint(
    path: Path,
    *,
    role: Literal["latest", "best", "final", "emergency"],
    run_id: str,
    model: PasteVolumeResNet,
    optimizer: torch.optim.Optimizer,
    scheduler: torch.optim.lr_scheduler.ReduceLROnPlateau,
    scaler: torch.GradScaler,
    state: _TrainState,
    config: TrainingCoreConfig,
    parent_run_id: str | None,
    parent_checkpoint_id: str | None,
    rng_state: Mapping[str, object] | None = None,
) -> None:
    _atomic_torch_save(
        _checkpoint_payload(
            role=role,
            run_id=run_id,
            model=model,
            optimizer=optimizer,
            scheduler=scheduler,
            scaler=scaler,
            state=state,
            config=config,
            parent_run_id=parent_run_id,
            parent_checkpoint_id=parent_checkpoint_id,
            rng_state=rng_state,
        ),
        path,
    )


def _is_better(
    metrics: _RegressionMetrics,
    state: _TrainState,
    min_delta: float,
) -> bool:
    if metrics.gaussian_nll < state.best_validation_nll - min_delta:
        return True
    return (
        abs(metrics.gaussian_nll - state.best_validation_nll) <= min_delta
        and metrics.mae_ul < state.best_validation_mae
    )


def _truncate_metric_history(
    raw_history: Sequence[object], state: _TrainState
) -> list[dict[str, object]]:
    """checkpointより先の履歴と同epochの重複を決定的に除く."""

    by_epoch: dict[int, dict[str, object]] = {}
    for raw_item in raw_history:
        if not isinstance(raw_item, Mapping):
            raise ValueError("existing learning curve epoch must be a mapping")
        item = dict(raw_item)
        epoch = item.get("epoch")
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
            raise ValueError("existing learning curve epoch is invalid")
        raw_step = item.get("global_step")
        if raw_step is not None and (
            not isinstance(raw_step, int) or isinstance(raw_step, bool) or raw_step < 0
        ):
            raise ValueError("existing learning curve global_step is invalid")
        if epoch >= state.epoch:
            continue
        if raw_step is not None and raw_step > state.global_step:
            continue
        by_epoch[epoch] = item
    return [by_epoch[epoch] for epoch in sorted(by_epoch)]


def train_model(
    config: TrainingCoreConfig,
    data: _TrainingData,
    logger: ExperimentLogger,
    *,
    run_artifacts: Sequence[tuple[Path, str | None]] = (),
    deadline_started_at: float | None = None,
    formal_tracking_uri: str | None = None,
) -> TrainResult:
    """明示loggerとdeterministic data sourceでモデルを学習する."""

    started_at = (
        deadline_started_at if deadline_started_at is not None else time.monotonic()
    )
    deadline = (
        started_at + config.trainer.deadline_seconds
        if config.trainer.deadline_seconds is not None
        else None
    )
    finalization_deadline = (
        deadline + config.trainer.finalization_grace_seconds
        if deadline is not None
        else None
    )
    _seed_everything(config.trainer.seed, deterministic=config.trainer.deterministic)
    device = _resolve_device(config.trainer.device)
    checkpoint_dir = config.checkpoint.directory.resolve()
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    latest_path = checkpoint_dir / "latest.ckpt"
    best_path = checkpoint_dir / "best.ckpt"
    final_path = checkpoint_dir / "final.ckpt"
    emergency_path = checkpoint_dir / "emergency.ckpt"
    weights_path = checkpoint_dir / "weights.pt"
    failure_path = checkpoint_dir / "failure.json"

    model = PasteVolumeResNet(config.model).to(device)
    parent_run_id: str | None = None
    parent_checkpoint_id: str | None = None
    parent_weights_sha256: str | None = None
    resume_payload: dict[str, object] | None = None
    if config.checkpoint.resume_checkpoint is not None:
        resume_payload = _load_training_checkpoint(config.checkpoint.resume_checkpoint)
        _validate_resume(resume_payload, config)
    if config.run_kind == "finetune":
        model.set_fine_tune_trainable(full_model=config.trainer.fine_tune_full_model)
    if resume_payload is None and config.checkpoint.initial_weights is not None:
        parent_run_id, parent_checkpoint_id, parent_weights_sha256 = (
            _load_initial_weights(
                config.checkpoint.initial_weights,
                model,
                config,
                formal_tracking_uri=formal_tracking_uri,
            )
        )

    optimizer = torch.optim.AdamW(
        (parameter for parameter in model.parameters() if parameter.requires_grad),
        lr=config.trainer.learning_rate,
        weight_decay=config.trainer.weight_decay,
    )
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer,
        factor=config.trainer.scheduler_factor,
        patience=config.trainer.scheduler_patience,
    )
    scaler = torch.GradScaler("cuda", enabled=device.type == "cuda")
    state = _TrainState()
    if resume_payload is not None:
        model.load_state_dict(
            cast(Mapping[str, Tensor], resume_payload["model_state"]), strict=True
        )
        optimizer.load_state_dict(
            cast(dict[str, object], resume_payload["optimizer_state"])
        )
        scheduler.load_state_dict(
            cast(dict[str, object], resume_payload["scheduler_state"])
        )
        scaler.load_state_dict(
            cast(dict[str, object], resume_payload["grad_scaler_state"])
        )
        state = _state_from_checkpoint(resume_payload)
        _restore_rng_state(cast(Mapping[str, object], resume_payload["rng_state"]))
        parent_run_id = cast(str | None, resume_payload.get("parent_run_id"))
        parent_checkpoint_id = cast(
            str | None, resume_payload.get("parent_checkpoint_id")
        )

    run_tags: dict[str, str | int] = {
        **dict(config.run_tags),
        "run_kind": config.run_kind,
        "dataset_fingerprint": config.dataset_fingerprint,
        "split_fingerprint": config.split_fingerprint,
        "training_protocol_fingerprint": config.training_protocol_fingerprint,
        "model_schema_version": 1,
    }
    if parent_run_id is not None:
        run_tags["parent_base_run_id"] = parent_run_id
    if parent_checkpoint_id is not None:
        run_tags["parent_checkpoint_id"] = parent_checkpoint_id
    if parent_weights_sha256 is not None:
        run_tags["parent_weights_sha256"] = parent_weights_sha256
    run_id = logger.start(
        run_kind=config.run_kind,
        tags=run_tags,
    )
    if resume_payload is not None and run_id != resume_payload["run_id"]:
        logger.end(status="FAILED")
        raise ValueError("resume must continue the original MLflow run ID")
    termination_guard = _TerminationSignals()
    diagnostics_directory = checkpoint_dir / "diagnostics"
    history_path = diagnostics_directory / "learning-curve.json"
    metric_history: list[dict[str, object]] = []
    active_group_start: int | None = None
    active_group_rng: Mapping[str, object] | None = None
    optimizer_boundary = OptimizerBoundary()

    def interrupt_for_signal() -> Never:
        _save_checkpoint(
            latest_path,
            role="latest",
            run_id=run_id,
            model=model,
            optimizer=optimizer,
            scheduler=scheduler,
            scaler=scaler,
            state=state,
            config=config,
            parent_run_id=parent_run_id,
            parent_checkpoint_id=parent_checkpoint_id,
        )
        logger.log_artifact(latest_path, artifact_path="checkpoints")
        logger.set_tags(
            {
                "stop_reason": "signal",
                "resume_checkpoint": str(latest_path),
            }
        )
        logger.flush()
        logger.end(status="KILLED")
        raise TrainingInterrupted(run_id=run_id, checkpoint=latest_path)

    def ensure_finalization_budget() -> None:
        if (
            finalization_deadline is not None
            and time.monotonic() >= finalization_deadline
        ):
            raise TimeoutError("training finalization deadline exceeded")

    def evaluate_with_signal_stop(
        evaluated_model: Callable[[Tensor, Tensor, Tensor], tuple[Tensor, Tensor]],
        *,
        termination: _TerminationSignals,
        log_variance_offset: float = 0.0,
    ) -> _EvaluationPredictions:
        try:
            return _evaluate_batches(
                evaluated_model,
                data,
                split="validation",
                device=device,
                log_variance_offset=log_variance_offset,
                deadline_monotonic=finalization_deadline,
                stop_requested=lambda: termination.requested,
            )
        except EvaluationStopRequested:
            interrupt_for_signal()

    try:
        termination_guard.__enter__()
        if history_path.is_file():
            history_payload = json.loads(history_path.read_text(encoding="utf-8"))
            if not isinstance(history_payload, dict) or not isinstance(
                history_payload.get("epochs"), list
            ):
                raise ValueError("existing learning curve artifact is invalid")
            metric_history = _truncate_metric_history(
                cast(list[object], history_payload["epochs"]), state
            )
            write_json_artifact(history_path, {"epochs": metric_history})
        logger.log_params(
            {
                "model.family": config.model.family,
                "model.input_channels": config.model.input_channels,
                "model.stem_channels": json.dumps(config.model.stem_channels),
                "model.stage_channels": json.dumps(config.model.stage_channels),
                "model.blocks_per_stage": json.dumps(config.model.blocks_per_stage),
                "model.group_norm_groups": config.model.group_norm_groups,
                "model.hidden_features": config.model.hidden_features,
                "model.log_variance_min": config.model.log_variance_min,
                "model.log_variance_max": config.model.log_variance_max,
                "model.parameter_count": model_parameter_count(model),
                "model.gmac_512x512": model_gmac(config.model),
                "optimizer.name": "AdamW",
                "scheduler.name": "ReduceLROnPlateau",
                "trainer.resolved_device": str(device),
                "trainer.precision": "cuda-amp"
                if device.type == "cuda"
                else "cpu-fp32",
                "trainer.learning_rate": config.trainer.learning_rate,
                "trainer.weight_decay": config.trainer.weight_decay,
                "trainer.gradient_accumulation_steps": config.trainer.gradient_accumulation_steps,
                "trainer.gradient_clip_norm": config.trainer.gradient_clip_norm,
                "trainer.max_epochs": config.trainer.max_epochs,
                "trainer.max_steps": config.trainer.max_steps or "null",
                "trainer.early_stopping_patience": config.trainer.early_stopping_patience,
                "trainer.early_stopping_min_delta": config.trainer.early_stopping_min_delta,
                "trainer.scheduler_factor": config.trainer.scheduler_factor,
                "trainer.scheduler_patience": config.trainer.scheduler_patience,
                "trainer.compile_enabled": config.trainer.compile_enabled,
                "trainer.compile_backend": config.trainer.compile_backend,
                "trainer.compile_mode": config.trainer.compile_mode,
                "trainer.deterministic": config.trainer.deterministic,
                "trainer.deadline_seconds": config.trainer.deadline_seconds or "null",
                "trainer.finalization_grace_seconds": config.trainer.finalization_grace_seconds,
                "trainer.fine_tune_full_model": config.trainer.fine_tune_full_model,
                "trainer.seed": config.trainer.seed,
                **dict(config.run_params),
            }
        )
        model_summary_path = diagnostics_directory / "model-summary.json"
        write_json_artifact(
            model_summary_path,
            {
                "model": config.model.to_dict(),
                "architecture": str(model),
                "parameter_count": model_parameter_count(model),
                "trainable_parameter_count": sum(
                    parameter.numel()
                    for parameter in model.parameters()
                    if parameter.requires_grad
                ),
                "gmac_512x512": model_gmac(config.model),
                "device": str(device),
                "precision": "cuda-amp" if device.type == "cuda" else "cpu-fp32",
            },
        )
        logger.log_artifact(model_summary_path, artifact_path="diagnostics")
        for artifact, artifact_path in run_artifacts:
            logger.log_artifact(artifact, artifact_path=artifact_path)
        forward_model: Callable[[Tensor, Tensor, Tensor], tuple[Tensor, Tensor]] = model
        if deadline is None or time.monotonic() < deadline:
            first_plan = data.training_batch_plan(state.epoch)
            if not first_plan:
                raise ValueError("train split has no batches")
            smoke_batch = data.training_batch(first_plan[0], epoch=state.epoch).to(
                device
            )
            validate_model_inputs(
                smoke_batch.image_6ch,
                smoke_batch.valid_pixel_mask,
                smoke_batch.pixel_per_mm,
            )
            optimizer.zero_grad(set_to_none=True)
            smoke_mean, smoke_log_variance = model(
                smoke_batch.image_6ch,
                smoke_batch.valid_pixel_mask,
                smoke_batch.pixel_per_mm,
            )
            smoke_loss = weighted_gaussian_nll(
                smoke_mean,
                smoke_log_variance,
                smoke_batch.target_volume_ul,
                smoke_batch.sample_weight,
            )
            validate_loss_inputs(
                smoke_mean,
                smoke_log_variance,
                smoke_batch.target_volume_ul,
                smoke_batch.sample_weight,
            )
            smoke_loss.backward()
            if not gradients_are_finite(model):
                raise FloatingPointError("eager smoke produced non-finite gradients")
            optimizer.zero_grad(set_to_none=True)
            if termination_guard.requested:
                interrupt_for_signal()
            compile_started = time.monotonic()
            compile_skipped_deadline = (
                deadline is not None and compile_started >= deadline
            )
            compile_graph_break_count = 0
            if config.trainer.compile_enabled and not compile_skipped_deadline:
                graph_breaks_before = torch_compile_graph_break_count()
                forward_model = cast(
                    Callable[[Tensor, Tensor, Tensor], tuple[Tensor, Tensor]],
                    torch.compile(
                        model,
                        backend=config.trainer.compile_backend,
                        mode=config.trainer.compile_mode,
                        fullgraph=False,
                        dynamic=None,
                    ),
                )
                compiled_mean, compiled_log_variance = forward_model(
                    smoke_batch.image_6ch,
                    smoke_batch.valid_pixel_mask,
                    smoke_batch.pixel_per_mm,
                )
                validate_loss_inputs(
                    compiled_mean,
                    compiled_log_variance,
                    smoke_batch.target_volume_ul,
                    smoke_batch.sample_weight,
                )
                compiled_loss = weighted_gaussian_nll(
                    compiled_mean,
                    compiled_log_variance,
                    smoke_batch.target_volume_ul,
                    smoke_batch.sample_weight,
                )
                compiled_loss.backward()
                if not gradients_are_finite(model):
                    raise FloatingPointError(
                        "torch.compile smoke produced non-finite gradients"
                    )
                optimizer.zero_grad(set_to_none=True)
                compile_graph_break_count = max(
                    0,
                    torch_compile_graph_break_count() - graph_breaks_before,
                )
            compile_elapsed = (
                time.monotonic() - compile_started
                if config.trainer.compile_enabled and not compile_skipped_deadline
                else 0.0
            )
            logger.log_metrics(
                {
                    "compile_seconds": compile_elapsed,
                    "compile_first_step_seconds": compile_elapsed,
                    "compile_skipped_deadline": float(compile_skipped_deadline),
                    "compile_graph_break_count": float(compile_graph_break_count),
                },
                step=state.global_step,
            )
            if compile_graph_break_count:
                raise RuntimeError(
                    "torch.compile training smoke detected "
                    f"{compile_graph_break_count} graph break(s); "
                    f"backend={config.trainer.compile_backend}, "
                    f"mode={config.trainer.compile_mode}"
                )
            if termination_guard.requested:
                interrupt_for_signal()
        else:
            logger.log_metrics(
                {
                    "compile_seconds": 0.0,
                    "compile_first_step_seconds": 0.0,
                    "compile_skipped_deadline": 1.0,
                    "compile_graph_break_count": 0.0,
                },
                step=state.global_step,
            )

        # Keep one known-good rollback point even before the first optimizer step.
        # Later interval checkpoints replace it only after a complete accumulation
        # group has committed.
        _save_checkpoint(
            latest_path,
            role="latest",
            run_id=run_id,
            model=model,
            optimizer=optimizer,
            scheduler=scheduler,
            scaler=scaler,
            state=state,
            config=config,
            parent_run_id=parent_run_id,
            parent_checkpoint_id=parent_checkpoint_id,
        )
        last_checkpoint_at = time.monotonic()
        stopped_reason: StopReason = "max_epochs"
        last_validation_metrics: _RegressionMetrics | None = None
        distinct_batch_shapes: set[tuple[int, int, int]] = set()
        first_optimizer_step_seconds: float | None = None
        steady_state_samples = 0
        steady_state_seconds = 0.0
        if device.type == "cuda":
            torch.cuda.reset_peak_memory_stats(device)
        with _TerminationSignals() as termination:
            termination.requested = termination_guard.requested
            while state.epoch < config.trainer.max_epochs:
                plan = data.training_batch_plan(state.epoch)
                if state.batch_plan and state.next_batch_index > 0:
                    if plan != state.batch_plan:
                        raise ValueError(
                            "regenerated batch plan does not match checkpoint"
                        )
                else:
                    state.batch_plan = plan
                    state.next_batch_index = 0
                if not plan:
                    raise ValueError("train split has no batches")
                model.train()
                epoch_started = time.monotonic()
                epoch_sample_count = 0
                epoch_metric_reducer = _RegressionMetricReducer()
                batch_index = state.next_batch_index
                epoch_was_interrupted = False
                while batch_index < len(plan):
                    if termination.requested:
                        interrupt_for_signal()
                    if deadline is not None and time.monotonic() >= deadline:
                        stopped_reason = "deadline"
                        epoch_was_interrupted = True
                        break
                    if (
                        config.trainer.max_steps is not None
                        and state.global_step >= config.trainer.max_steps
                    ):
                        stopped_reason = "max_steps"
                        epoch_was_interrupted = True
                        break
                    group_start = batch_index
                    group_rng = _rng_state()
                    active_group_start = group_start
                    active_group_rng = group_rng
                    optimizer_boundary.in_progress = False
                    group_size = min(
                        config.trainer.gradient_accumulation_steps,
                        len(plan) - group_start,
                    )

                    def group_batches() -> Iterator[_TrainingBatch]:
                        for offset in range(group_size):
                            if deadline is not None and time.monotonic() >= deadline:
                                return
                            batch_ids = plan[group_start + offset]
                            yield data.training_batch(batch_ids, epoch=state.epoch).to(
                                device
                            )

                    def training_step(
                        batch: _TrainingBatch,
                    ) -> TrainingStepResult[_TrainingObservation]:
                        validate_model_inputs(
                            batch.image_6ch,
                            batch.valid_pixel_mask,
                            batch.pixel_per_mm,
                        )
                        mean, log_variance = forward_model(
                            batch.image_6ch,
                            batch.valid_pixel_mask,
                            batch.pixel_per_mm,
                        )
                        loss = weighted_gaussian_nll(
                            mean,
                            log_variance,
                            batch.target_volume_ul,
                            batch.sample_weight,
                        )
                        distinct_batch_shapes.add(
                            (
                                int(batch.image_6ch.shape[0]),
                                int(batch.image_6ch.shape[2]),
                                int(batch.image_6ch.shape[3]),
                            )
                        )
                        return TrainingStepResult(
                            loss=loss,
                            observation=_TrainingObservation(
                                mean=mean.detach().cpu(),
                                log_variance=log_variance.detach().cpu(),
                                target=batch.target_volume_ul.detach().cpu(),
                                sample_weight=batch.sample_weight.detach().cpu(),
                            ),
                            sample_count=len(batch.sample_ids),
                        )

                    group_result = execute_optimizer_group(
                        group_batches(),
                        expected_batch_count=group_size,
                        model=model,
                        optimizer=optimizer,
                        scaler=scaler,
                        gradient_clip_norm=config.trainer.gradient_clip_norm,
                        device_type=device.type,
                        amp_enabled=device.type == "cuda",
                        step=training_step,
                        metric_reducer=epoch_metric_reducer,
                        can_commit=lambda: (
                            deadline is None or time.monotonic() < deadline
                        ),
                        boundary=optimizer_boundary,
                    )
                    batch_index = group_start + group_result.processed_batch_count
                    if not group_result.committed:
                        _restore_rng_state(group_rng)
                        batch_index = group_start
                        state.next_batch_index = group_start
                        active_group_start = None
                        active_group_rng = None
                        stopped_reason = "deadline"
                        epoch_was_interrupted = True
                        break
                    epoch_sample_count += group_result.sample_count
                    group_elapsed = group_result.elapsed_seconds
                    if first_optimizer_step_seconds is None:
                        first_optimizer_step_seconds = group_elapsed
                    else:
                        steady_state_samples += group_result.sample_count
                        steady_state_seconds += group_elapsed
                    state.global_step += 1
                    state.next_batch_index = batch_index
                    active_group_start = None
                    active_group_rng = None
                    should_save = (
                        state.global_step % config.checkpoint.save_interval_steps == 0
                        or time.monotonic() - last_checkpoint_at
                        >= config.checkpoint.save_interval_seconds
                    )
                    if should_save:
                        _save_checkpoint(
                            latest_path,
                            role="latest",
                            run_id=run_id,
                            model=model,
                            optimizer=optimizer,
                            scheduler=scheduler,
                            scaler=scaler,
                            state=state,
                            config=config,
                            parent_run_id=parent_run_id,
                            parent_checkpoint_id=parent_checkpoint_id,
                        )
                        last_checkpoint_at = time.monotonic()
                    if termination.requested:
                        interrupt_for_signal()

                if stopped_reason == "deadline":
                    _save_checkpoint(
                        latest_path,
                        role="latest",
                        run_id=run_id,
                        model=model,
                        optimizer=optimizer,
                        scheduler=scheduler,
                        scaler=scaler,
                        state=state,
                        config=config,
                        parent_run_id=parent_run_id,
                        parent_checkpoint_id=parent_checkpoint_id,
                    )
                    ensure_finalization_budget()
                train_elapsed = time.monotonic() - epoch_started
                train_metrics = epoch_metric_reducer.metrics()
                model.eval()
                validation = evaluate_with_signal_stop(
                    forward_model,
                    termination=termination,
                )
                if termination.requested:
                    interrupt_for_signal()
                last_validation_metrics = validation.metrics
                resource_usage = resource.getrusage(resource.RUSAGE_SELF)
                system_metrics = {
                    "first_optimizer_step_seconds": float(
                        first_optimizer_step_seconds or 0.0
                    ),
                    "steady_state_samples_per_second": steady_state_samples
                    / max(steady_state_seconds, 1e-12),
                    "distinct_batch_shape_count": float(len(distinct_batch_shapes)),
                    "process_peak_rss_mb": float(resource_usage.ru_maxrss) / 1024.0,
                }
                if device.type == "cuda":
                    system_metrics["peak_gpu_memory_mb"] = float(
                        torch.cuda.max_memory_allocated(device)
                    ) / (1024.0 * 1024.0)
                epoch_metrics = {
                    **(
                        train_metrics.to_dict("train/")
                        if train_metrics is not None
                        else {}
                    ),
                    **validation.metrics.to_dict("validation/"),
                    **system_metrics,
                    "learning_rate": float(optimizer.param_groups[0]["lr"]),
                    "epoch_seconds": train_elapsed,
                    "train_samples_per_second": epoch_sample_count
                    / max(train_elapsed, 1e-12),
                }
                logger.log_metrics(epoch_metrics, step=state.epoch)
                metric_history.append(
                    {
                        "epoch": state.epoch,
                        "global_step": state.global_step,
                        **epoch_metrics,
                    }
                )
                write_json_artifact(history_path, {"epochs": metric_history})
                if not epoch_was_interrupted:
                    scheduler.step(validation.metrics.gaussian_nll)
                    improved = _is_better(
                        validation.metrics,
                        state,
                        config.trainer.early_stopping_min_delta,
                    )
                    if improved:
                        state.best_validation_nll = validation.metrics.gaussian_nll
                        state.best_validation_mae = validation.metrics.mae_ul
                        state.best_validation_metrics = validation.metrics
                        state.best_epoch = state.epoch
                        state.patience_counter = 0
                    else:
                        state.patience_counter += 1
                    state.epoch += 1
                    state.epochs_completed += 1
                    state.next_batch_index = 0
                    state.batch_plan = ()
                    if improved:
                        _save_checkpoint(
                            best_path,
                            role="best",
                            run_id=run_id,
                            model=model,
                            optimizer=optimizer,
                            scheduler=scheduler,
                            scaler=scaler,
                            state=state,
                            config=config,
                            parent_run_id=parent_run_id,
                            parent_checkpoint_id=parent_checkpoint_id,
                        )
                elif not best_path.is_file() and state.global_step > 0:
                    # Publish the evaluated terminating weights without mutating the
                    # scheduler/early-stop state stored in resumeable checkpoints.
                    _save_checkpoint(
                        best_path,
                        role="best",
                        run_id=run_id,
                        model=model,
                        optimizer=optimizer,
                        scheduler=scheduler,
                        scaler=scaler,
                        state=state,
                        config=config,
                        parent_run_id=parent_run_id,
                        parent_checkpoint_id=parent_checkpoint_id,
                    )
                _save_checkpoint(
                    latest_path,
                    role="latest",
                    run_id=run_id,
                    model=model,
                    optimizer=optimizer,
                    scheduler=scheduler,
                    scaler=scaler,
                    state=state,
                    config=config,
                    parent_run_id=parent_run_id,
                    parent_checkpoint_id=parent_checkpoint_id,
                )
                if epoch_was_interrupted:
                    break
                if state.patience_counter >= config.trainer.early_stopping_patience:
                    stopped_reason = "early_stopping"
                    break
                if (
                    config.trainer.max_steps is not None
                    and state.global_step >= config.trainer.max_steps
                ):
                    stopped_reason = "max_steps"
                    break
            if termination.requested:
                interrupt_for_signal()
            if stopped_reason == "deadline" and not best_path.is_file():
                logger.log_artifact(latest_path, artifact_path="checkpoints")
                logger.set_tags(
                    {
                        "stop_reason": "deadline_before_first_step",
                        "resume_checkpoint": str(latest_path),
                    }
                )
                logger.flush()
                logger.end(status="KILLED")
                raise TrainingDeadlineExceeded(run_id=run_id, checkpoint=latest_path)
            ensure_finalization_budget()

        if termination_guard.requested:
            interrupt_for_signal()
        finalization_started_at = time.monotonic()
        ensure_finalization_budget()
        best_metrics = state.best_validation_metrics or last_validation_metrics
        if not best_path.is_file() or best_metrics is None:
            raise RuntimeError("training completed without a best checkpoint")
        best_payload = _load_training_checkpoint(best_path)
        calibration_model = PasteVolumeResNet(config.model).to(device)
        calibration_model.load_state_dict(
            cast(Mapping[str, Tensor], best_payload["model_state"]), strict=True
        )
        calibration_model.eval()
        validation_uncalibrated = evaluate_with_signal_stop(
            calibration_model,
            termination=termination_guard,
        )
        if termination_guard.requested:
            interrupt_for_signal()
        ensure_finalization_budget()
        offset = _fit_log_variance_offset(validation_uncalibrated)
        validation_calibrated = evaluate_with_signal_stop(
            calibration_model,
            termination=termination_guard,
            log_variance_offset=offset,
        )
        if termination_guard.requested:
            interrupt_for_signal()
        ensure_finalization_budget()
        _save_checkpoint(
            final_path,
            role="final",
            run_id=run_id,
            model=model,
            optimizer=optimizer,
            scheduler=scheduler,
            scaler=scaler,
            state=state,
            config=config,
            parent_run_id=parent_run_id,
            parent_checkpoint_id=parent_checkpoint_id,
        )
        if termination_guard.requested:
            interrupt_for_signal()
        ensure_finalization_budget()
        for calibrated_checkpoint in (best_path, final_path):
            ensure_finalization_budget()
            calibrated_payload = _load_training_checkpoint(calibrated_checkpoint)
            calibrated_payload["uncertainty_log_variance_offset"] = offset
            _atomic_torch_save(calibrated_payload, calibrated_checkpoint)
        _extract_best_weights(
            best_path,
            weights_path,
            uncertainty_log_variance_offset=offset,
        )
        prediction_path = diagnostics_directory / "validation-predictions.json"
        means = [
            float(value) for value in validation_calibrated.mean_volume_ul.flatten()
        ]
        log_variances = [
            float(value)
            for value in validation_calibrated.log_variance_volume_ul2.flatten()
        ]
        targets = [
            float(value) for value in validation_calibrated.target_volume_ul.flatten()
        ]
        write_json_artifact(
            prediction_path,
            {
                "log_variance_offset": offset,
                "metrics": asdict(validation_calibrated.metrics),
                "predictions": [
                    {
                        "sample_id": sample_id,
                        "target_volume_ul": target,
                        "mean_volume_ul": mean,
                        "log_variance_volume_ul2": log_variance,
                        "std_volume_ul": math.exp(0.5 * log_variance),
                        "residual_ul": mean - target,
                    }
                    for sample_id, target, mean, log_variance in zip(
                        validation_calibrated.sample_ids,
                        targets,
                        means,
                        log_variances,
                        strict=True,
                    )
                ],
            },
        )
        diagnostic_artifacts = [model_summary_path, history_path, prediction_path]
        if metric_history:
            learning_curve_path = diagnostics_directory / "learning-curve.svg"
            _write_scatter_svg(
                learning_curve_path,
                x_values=[float(cast(Any, item["epoch"])) for item in metric_history],
                y_values=[
                    float(cast(Any, item["validation/gaussian_nll"]))
                    for item in metric_history
                ],
                title="Validation Gaussian NLL",
                x_label="epoch",
                y_label="Gaussian NLL",
                connect=True,
            )
            diagnostic_artifacts.append(learning_curve_path)
        selected_indices = sorted(
            range(len(validation_calibrated.sample_ids)),
            key=lambda index: validation_calibrated.sample_ids[index],
        )[:200]
        selected_targets = [targets[index] for index in selected_indices]
        selected_means = [means[index] for index in selected_indices]
        prediction_plot_path = diagnostics_directory / "prediction-vs-target.svg"
        _write_scatter_svg(
            prediction_plot_path,
            x_values=selected_targets,
            y_values=selected_means,
            title="Prediction vs Target",
            x_label="target [uL]",
            y_label="prediction [uL]",
            reference="diagonal",
        )
        diagnostic_artifacts.append(prediction_plot_path)
        residual_plot_path = diagnostics_directory / "residual.svg"
        _write_scatter_svg(
            residual_plot_path,
            x_values=selected_targets,
            y_values=[
                mean - target
                for mean, target in zip(selected_means, selected_targets, strict=True)
            ],
            title="Residual vs Target",
            x_label="target [uL]",
            y_label="residual [uL]",
            reference="zero",
        )
        diagnostic_artifacts.append(residual_plot_path)
        normalized_errors = sorted(
            (means[index] - targets[index]) / math.exp(0.5 * log_variances[index])
            for index in selected_indices
        )
        coverage_plot_path = diagnostics_directory / "coverage-qq.svg"
        _write_scatter_svg(
            coverage_plot_path,
            x_values=[
                NormalDist().inv_cdf((index + 0.5) / len(normalized_errors))
                for index in range(len(normalized_errors))
            ],
            y_values=normalized_errors,
            title="Normalized Residual Q-Q",
            x_label="standard normal quantile",
            y_label="normalized residual",
            reference="diagonal",
        )
        diagnostic_artifacts.append(coverage_plot_path)
        for artifact in (latest_path, best_path, final_path, weights_path):
            ensure_finalization_budget()
            logger.log_artifact(artifact, artifact_path="checkpoints")
        for artifact in diagnostic_artifacts:
            ensure_finalization_budget()
            logger.log_artifact(artifact, artifact_path="diagnostics")
        logger.log_metrics(
            {
                **validation_calibrated.metrics.to_dict("validation_calibrated/"),
                "total_elapsed_seconds": time.monotonic() - started_at,
                "finalization_seconds": time.monotonic() - finalization_started_at,
            },
            step=state.global_step,
        )
        ensure_finalization_budget()
        logger.flush()
        ensure_finalization_budget()
        if termination_guard.requested:
            interrupt_for_signal()
        if formal_tracking_uri is None:
            logger.end(status="FINISHED")
        else:
            publish_formal_artifact(
                weights_path,
                tracking_uri=formal_tracking_uri,
                run_kind=config.run_kind,
                logger=logger,
            )
        return TrainResult(
            run_id=run_id,
            latest_checkpoint=latest_path,
            best_checkpoint=best_path,
            final_checkpoint=final_path,
            weights_path=weights_path,
            epochs_completed=state.epochs_completed,
            global_step=state.global_step,
            stopped_reason=stopped_reason,
            best_validation_metrics=best_metrics,
            calibrated_validation_metrics=validation_calibrated.metrics,
            log_variance_offset=offset,
            dataset_fingerprint=config.dataset_fingerprint,
            split_fingerprint=config.split_fingerprint,
            parent_run_id=parent_run_id,
            parent_checkpoint_id=parent_checkpoint_id,
        )
    except (TrainingDeadlineExceeded, TrainingInterrupted):
        raise
    except Exception as error:
        failure_rng: Mapping[str, object] | None = None
        try:
            optimizer.zero_grad(set_to_none=True)
            if active_group_start is not None:
                if optimizer_boundary.in_progress:
                    # An optimizer update is not assumed atomic.  Fall back to the
                    # most recent fully committed checkpoint rather than persisting
                    # a possibly half-updated parameter/optimizer pair.
                    safe_payload = _load_training_checkpoint(latest_path)
                    model.load_state_dict(
                        cast(Mapping[str, Tensor], safe_payload["model_state"]),
                        strict=True,
                    )
                    optimizer.load_state_dict(
                        cast(dict[str, object], safe_payload["optimizer_state"])
                    )
                    scheduler.load_state_dict(
                        cast(dict[str, object], safe_payload["scheduler_state"])
                    )
                    scaler.load_state_dict(
                        cast(dict[str, object], safe_payload["grad_scaler_state"])
                    )
                    state = _state_from_checkpoint(safe_payload)
                    _restore_rng_state(
                        cast(Mapping[str, object], safe_payload["rng_state"])
                    )
                else:
                    if active_group_rng is None:
                        raise AssertionError(
                            "active accumulation group has no RNG rollback state"
                        )
                    state.next_batch_index = active_group_start
                    _restore_rng_state(active_group_rng)
            failure_rng = _rng_state()
            for checkpoint_path, role in (
                (latest_path, "latest"),
                (emergency_path, "emergency"),
            ):
                _save_checkpoint(
                    checkpoint_path,
                    role=cast(Literal["latest", "best", "final", "emergency"], role),
                    run_id=run_id,
                    model=model,
                    optimizer=optimizer,
                    scheduler=scheduler,
                    scaler=scaler,
                    state=state,
                    config=config,
                    parent_run_id=parent_run_id,
                    parent_checkpoint_id=parent_checkpoint_id,
                    rng_state=failure_rng,
                )
            if metric_history:
                write_json_artifact(history_path, {"epochs": metric_history})
            write_json_artifact(
                failure_path,
                {
                    "exception_type": type(error).__name__,
                    "message": str(error),
                    "global_step": state.global_step,
                    "epoch": state.epoch,
                    "latest_checkpoint": str(latest_path),
                    "emergency_checkpoint": str(emergency_path),
                },
            )
        except Exception:
            pass
        try:
            logger.set_tags(
                {
                    "failure_reason": type(error).__name__,
                    "failure_message": str(error)[:500],
                }
            )
            logger.log_artifact(latest_path, artifact_path="checkpoints")
            if emergency_path.is_file():
                logger.log_artifact(emergency_path, artifact_path="checkpoints")
            if history_path.is_file():
                logger.log_artifact(history_path, artifact_path="diagnostics")
            if failure_path.is_file():
                logger.log_artifact(failure_path, artifact_path="failure")
        except Exception:
            pass
        finally:
            try:
                logger.end(status="FAILED")
            except Exception:
                pass
        raise
    finally:
        termination_guard.__exit__(None, None, None)


__all__ = [
    "CheckpointConfig",
    "TrainResult",
    "TrainerConfig",
    "TrainingCoreConfig",
    "TrainingDeadlineExceeded",
    "TrainingInterrupted",
    "train_model",
]
