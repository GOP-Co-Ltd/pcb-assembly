"""Framework非依存のpure PyTorch学習、評価、checkpoint core."""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import resource
import signal
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import NormalDist
from typing import Any, Literal, Never, Protocol, cast, override

import numpy as np
import torch
from torch import Tensor, nn

from .compile_parity import torch_compile_graph_break_count
from .experiment import ExperimentLogger, Scalar, write_json_artifact
from .formal_artifact import (
    FormalArtifactAttestation,
    publish_formal_artifact,
    verify_formal_artifact,
)
from .model import (
    PasteVolumeModelConfig,
    PasteVolumeResNet,
    model_gmac,
    model_parameter_count,
    validate_loss_inputs,
    validate_model_inputs,
    weighted_gaussian_nll,
)

CHECKPOINT_KIND = "pcbasm-paste-volume-training-checkpoint"
CHECKPOINT_SCHEMA_VERSION = 1
WEIGHT_KIND = "paste-volume-model-weights"
WEIGHT_SCHEMA_VERSION = 1
_CHECKPOINT_ROLES = frozenset({"latest", "best", "final", "emergency"})
_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")

type RunKind = Literal["base-train", "finetune"]
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


class _EvaluationStopRequested(RuntimeError):
    """Evaluation batch境界で中断要求を検出したことを示す."""


@dataclass(frozen=True)
class TrainingBatch:
    """学習coreへ渡す1 batch."""

    image_6ch: Tensor
    valid_pixel_mask: Tensor
    pixel_per_mm: Tensor
    target_volume_ul: Tensor
    sample_weight: Tensor
    sample_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        batch_size = self.image_6ch.shape[0]
        if len(self.sample_ids) != batch_size:
            raise ValueError("sample_ids and tensor batch size must match")
        expected = (batch_size, 1)
        for name, tensor in (
            ("pixel_per_mm", self.pixel_per_mm),
            ("target_volume_ul", self.target_volume_ul),
            ("sample_weight", self.sample_weight),
        ):
            if tensor.shape != expected:
                raise ValueError(f"{name} must have shape [B, 1]")

    def to(self, device: torch.device) -> TrainingBatch:
        return TrainingBatch(
            image_6ch=self.image_6ch.to(device),
            valid_pixel_mask=self.valid_pixel_mask.to(device),
            pixel_per_mm=self.pixel_per_mm.to(device),
            target_volume_ul=self.target_volume_ul.to(device),
            sample_weight=self.sample_weight.to(device),
            sample_ids=self.sample_ids,
        )


class TrainingData(Protocol):
    """Deterministic batch planとtensor materializeを分離する境界."""

    def training_batch_plan(self, epoch: int) -> tuple[tuple[str, ...], ...]: ...

    def training_batch(
        self, sample_ids: tuple[str, ...], *, epoch: int
    ) -> TrainingBatch: ...

    def evaluation_batch_plan(
        self, split: Literal["validation", "test"]
    ) -> tuple[tuple[str, ...], ...]: ...

    def evaluation_batch(
        self,
        sample_ids: tuple[str, ...],
        *,
        split: Literal["validation", "test"],
    ) -> TrainingBatch: ...


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
    run_kind: RunKind
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
class RegressionMetrics:
    gaussian_nll: float
    mae_ul: float
    rmse_ul: float
    normalized_error_mean: float
    normalized_error_std: float
    normalized_error_score: float
    median_absolute_relative_error: float
    p95_absolute_relative_error: float
    one_std_coverage: float
    mean_prediction_std_ul: float
    invalid_prediction_count: int
    sample_count: int

    def to_dict(self, prefix: str = "") -> dict[str, float]:
        values = asdict(self)
        return {f"{prefix}{key}": float(value) for key, value in values.items()}


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
    best_validation_metrics: RegressionMetrics
    calibrated_validation_metrics: RegressionMetrics
    log_variance_offset: float
    dataset_fingerprint: str
    split_fingerprint: str
    parent_run_id: str | None
    parent_checkpoint_id: str | None


@dataclass(frozen=True)
class FormalTrainingWeights:
    """MLflow上の正式な学習runへ厳密に結び付いたweights."""

    weights_path: Path
    weights_sha256: str
    run_kind: RunKind
    source_run_id: str
    dataset_fingerprint: str
    split_fingerprint: str
    training_protocol_fingerprint: str
    attestation: FormalArtifactAttestation

    def __post_init__(self) -> None:
        if not self.weights_path.is_absolute():
            raise ValueError("formal training weights path must be absolute")
        for name, value in (
            ("weights_sha256", self.weights_sha256),
            ("dataset_fingerprint", self.dataset_fingerprint),
            ("split_fingerprint", self.split_fingerprint),
            (
                "training_protocol_fingerprint",
                self.training_protocol_fingerprint,
            ),
        ):
            if _SHA256_PATTERN.fullmatch(value) is None:
                raise ValueError(f"formal training weights {name} is invalid")
        if self.run_kind not in ("base-train", "finetune"):
            raise ValueError("formal training weights run_kind is invalid")
        if not self.source_run_id:
            raise ValueError("formal training weights source_run_id is required")
        if (
            self.attestation.output_path != self.weights_path
            or self.attestation.output_kind != "file"
            or self.attestation.output_fingerprint != self.weights_sha256
            or self.attestation.run_kind != self.run_kind
            or self.attestation.run_id != self.source_run_id
            or self.attestation.status != "FINISHED"
        ):
            raise ValueError(
                "formal training weights attestation does not match its lineage"
            )


@dataclass
class _TrainState:
    epoch: int = 0
    global_step: int = 0
    next_batch_index: int = 0
    batch_plan: tuple[tuple[str, ...], ...] = ()
    best_validation_nll: float = math.inf
    best_validation_mae: float = math.inf
    best_validation_metrics: RegressionMetrics | None = None
    best_epoch: int = -1
    patience_counter: int = 0
    epochs_completed: int = 0


class _TerminationSignals(AbstractContextManager["_TerminationSignals"]):
    def __init__(self) -> None:
        self.requested = False
        self._previous: dict[signal.Signals, Any] = {}

    @override
    def __enter__(self) -> _TerminationSignals:
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


def _resolve_device(value: str) -> torch.device:
    if value == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if value == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but is not available")
    return torch.device(value)


def seed_everything(seed: int, *, deterministic: bool) -> None:
    """Python、NumPy、PyTorchへ同じrun seedを設定する."""

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    torch.use_deterministic_algorithms(deterministic)


def _rng_state() -> dict[str, object]:
    numpy_state = cast(tuple[str, np.ndarray, int, int, float], np.random.get_state())
    return {
        "python": random.getstate(),
        "numpy_bit_generator": numpy_state[0],
        "numpy_state": torch.from_numpy(numpy_state[1].copy()),
        "numpy_position": int(numpy_state[2]),
        "numpy_has_gaussian": bool(numpy_state[3]),
        "numpy_cached_gaussian": float(numpy_state[4]),
        "torch_cpu": torch.get_rng_state(),
        "torch_cuda": torch.cuda.get_rng_state_all()
        if torch.cuda.is_available()
        else [],
    }


def _restore_rng_state(state: Mapping[str, object]) -> None:
    random.setstate(cast(tuple[object, ...], state["python"]))
    numpy_tensor = cast(Tensor, state["numpy_state"])
    np.random.set_state(
        (
            cast(str, state["numpy_bit_generator"]),
            numpy_tensor.cpu().numpy().astype(np.uint32, copy=False),
            cast(int, state["numpy_position"]),
            cast(bool, state["numpy_has_gaussian"]),
            cast(float, state["numpy_cached_gaussian"]),
        )
    )
    torch.set_rng_state(cast(Tensor, state["torch_cpu"]))
    cuda_states = cast(list[Tensor], state["torch_cuda"])
    if cuda_states:
        if not torch.cuda.is_available():
            raise ValueError("checkpoint contains CUDA RNG state on a CPU-only host")
        torch.cuda.set_rng_state_all(cuda_states)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def atomic_torch_save(payload: Mapping[str, object], path: Path) -> None:
    """同一directoryの一時fileを検証・fsync後、atomic replaceする."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            torch.save(dict(payload), stream)
            stream.flush()
            os.fsync(stream.fileno())
        loaded = torch.load(temporary, map_location="cpu", weights_only=True)
        if not isinstance(loaded, Mapping):
            raise ValueError("checkpoint write verification did not produce a mapping")
        _validate_torch_artifact(loaded)
        _validate_torch_artifact_identity(payload, loaded)
        _validate_torch_artifact_destination(loaded, path)
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_torch_artifact(payload: Mapping[str, object]) -> None:
    kind = payload.get("kind")
    if kind == CHECKPOINT_KIND:
        required = {
            "kind",
            "schema_version",
            "checkpoint_role",
            "created_unix_seconds",
            "run_id",
            "model_config",
            "model_state",
            "optimizer_state",
            "scheduler_state",
            "grad_scaler_state",
            "epoch",
            "global_step",
            "next_batch_index",
            "epochs_completed",
            "batch_plan",
            "best_selection_state",
            "best_validation_metrics",
            "rng_state",
            "dataset_fingerprint",
            "split_fingerprint",
            "config_fingerprint",
            "training_protocol_fingerprint",
            "resolved_config",
            "preprocess_schema",
            "uncertainty_log_variance_offset",
            "train_sample_ids",
            "trainer_config",
            "run_kind",
            "parent_run_id",
            "parent_checkpoint_id",
        }
        missing = sorted(required - payload.keys())
        unknown = sorted(payload.keys() - required)
        if missing or unknown:
            raise ValueError(
                "training checkpoint key set is invalid; "
                f"missing={missing}, unknown={unknown}"
            )
        if payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("unsupported training checkpoint schema")
        if payload.get("checkpoint_role") not in _CHECKPOINT_ROLES:
            raise ValueError("invalid training checkpoint role")
        created = payload["created_unix_seconds"]
        if (
            not isinstance(created, (int, float))
            or isinstance(created, bool)
            or not math.isfinite(float(created))
            or float(created) < 0
        ):
            raise ValueError("training checkpoint created_unix_seconds is invalid")
        for key in (
            "model_config",
            "model_state",
            "optimizer_state",
            "scheduler_state",
            "grad_scaler_state",
            "best_selection_state",
            "rng_state",
            "resolved_config",
            "preprocess_schema",
            "trainer_config",
        ):
            if not isinstance(payload[key], Mapping):
                raise ValueError(f"training checkpoint {key} must be a mapping")
        for key in ("epoch", "global_step", "next_batch_index", "epochs_completed"):
            value = payload[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"training checkpoint {key} must be non-negative")
        if not isinstance(payload["run_id"], str) or not payload["run_id"]:
            raise ValueError("training checkpoint run_id is required")
        protocol = payload["training_protocol_fingerprint"]
        if not isinstance(protocol, str) or not _SHA256_PATTERN.fullmatch(protocol):
            raise ValueError(
                "training checkpoint training_protocol_fingerprint is invalid"
            )
        for key in (
            "dataset_fingerprint",
            "split_fingerprint",
            "config_fingerprint",
        ):
            value = payload[key]
            if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
                raise ValueError(f"training checkpoint {key} is invalid")
        parent_run = payload["parent_run_id"]
        parent_checkpoint = payload["parent_checkpoint_id"]
        if (parent_run is None) != (parent_checkpoint is None):
            raise ValueError("training checkpoint parent lineage is incomplete")
        if parent_run is not None and (
            not isinstance(parent_run, str)
            or not parent_run
            or not isinstance(parent_checkpoint, str)
            or not _SHA256_PATTERN.fullmatch(parent_checkpoint)
        ):
            raise ValueError("training checkpoint parent lineage is invalid")
        if payload["run_kind"] not in ("base-train", "finetune"):
            raise ValueError("training checkpoint run_kind is invalid")
        if not isinstance(payload["batch_plan"], Sequence) or not isinstance(
            payload["train_sample_ids"], Sequence
        ):
            raise ValueError("training checkpoint sample plans must be sequences")
        offset = payload["uncertainty_log_variance_offset"]
        if offset is not None and (
            not isinstance(offset, (int, float)) or not math.isfinite(float(offset))
        ):
            raise ValueError("training checkpoint uncertainty offset is invalid")
        return
    if kind == WEIGHT_KIND:
        required = {
            "weight_schema_version",
            "kind",
            "model_config",
            "state_dict",
            "preprocess_schema",
            "uncertainty_log_variance_offset",
            "source_run_id",
            "source_checkpoint_role",
            "source_checkpoint_sha256",
            "dataset_fingerprint",
            "split_fingerprint",
            "training_protocol_fingerprint",
            "run_kind",
            "parent_run_id",
            "parent_checkpoint_id",
        }
        missing = sorted(required - payload.keys())
        unknown = sorted(payload.keys() - required)
        if missing or unknown:
            raise ValueError(
                "model weights key set is invalid; "
                f"missing={missing}, unknown={unknown}"
            )
        if payload.get("weight_schema_version") != WEIGHT_SCHEMA_VERSION:
            raise ValueError("unsupported model weights schema")
        if payload.get("source_checkpoint_role") != "best":
            raise ValueError("model weights must originate from a best checkpoint")
        for key in ("model_config", "state_dict", "preprocess_schema"):
            if not isinstance(payload[key], Mapping):
                raise ValueError(f"model weights {key} must be a mapping")
        offset = payload["uncertainty_log_variance_offset"]
        if not isinstance(offset, (int, float)) or not math.isfinite(float(offset)):
            raise ValueError("model weights uncertainty offset is invalid")
        for key in (
            "source_run_id",
            "source_checkpoint_sha256",
            "dataset_fingerprint",
            "split_fingerprint",
            "training_protocol_fingerprint",
        ):
            if not isinstance(payload[key], str) or not payload[key]:
                raise ValueError(f"model weights {key} is required")
        if not _SHA256_PATTERN.fullmatch(
            cast(str, payload["training_protocol_fingerprint"])
        ):
            raise ValueError("model weights training_protocol_fingerprint is invalid")
        if payload["run_kind"] not in ("base-train", "finetune"):
            raise ValueError("model weights run_kind is invalid")
        for key in (
            "source_checkpoint_sha256",
            "dataset_fingerprint",
            "split_fingerprint",
        ):
            if not _SHA256_PATTERN.fullmatch(cast(str, payload[key])):
                raise ValueError(f"model weights {key} is invalid")
        parent_run = payload["parent_run_id"]
        parent_checkpoint = payload["parent_checkpoint_id"]
        if (parent_run is None) != (parent_checkpoint is None):
            raise ValueError("model weights parent lineage is incomplete")
        if parent_run is not None and (
            not isinstance(parent_run, str)
            or not parent_run
            or not isinstance(parent_checkpoint, str)
            or not _SHA256_PATTERN.fullmatch(parent_checkpoint)
        ):
            raise ValueError("model weights parent lineage is invalid")
        return
    raise ValueError("unsupported torch artifact kind")


def _validate_torch_artifact_identity(
    expected: Mapping[str, object], loaded: Mapping[str, object]
) -> None:
    """readbackが意図したartifact種別とlineageを保ったことを確認する."""

    if expected.get("kind") == CHECKPOINT_KIND:
        identity_keys = (
            "kind",
            "schema_version",
            "checkpoint_role",
            "created_unix_seconds",
            "run_id",
            "dataset_fingerprint",
            "split_fingerprint",
            "config_fingerprint",
            "training_protocol_fingerprint",
            "run_kind",
            "parent_run_id",
            "parent_checkpoint_id",
            "epoch",
            "global_step",
            "next_batch_index",
            "epochs_completed",
        )
        state_key = "model_state"
    elif expected.get("kind") == WEIGHT_KIND:
        identity_keys = (
            "kind",
            "weight_schema_version",
            "source_checkpoint_role",
            "source_run_id",
            "source_checkpoint_sha256",
            "dataset_fingerprint",
            "split_fingerprint",
            "training_protocol_fingerprint",
            "run_kind",
            "parent_run_id",
            "parent_checkpoint_id",
            "uncertainty_log_variance_offset",
        )
        state_key = "state_dict"
    else:
        raise ValueError("unsupported torch artifact kind")
    for key in identity_keys:
        if loaded.get(key) != expected.get(key):
            raise ValueError(f"torch artifact readback changed {key}")
    expected_state = cast(Mapping[str, object], expected[state_key])
    loaded_state = cast(Mapping[str, object], loaded[state_key])
    if tuple(expected_state) != tuple(loaded_state):
        raise ValueError("torch artifact readback changed model state keys")


def _validate_torch_artifact_destination(
    payload: Mapping[str, object], path: Path
) -> None:
    expected_role = {
        "latest.ckpt": "latest",
        "best.ckpt": "best",
        "final.ckpt": "final",
        "emergency.ckpt": "emergency",
    }.get(path.name)
    if expected_role is not None and payload.get("checkpoint_role") != expected_role:
        raise ValueError(f"{path.name} requires checkpoint_role={expected_role}")
    if path.name == "weights.pt" and payload.get("kind") != WEIGHT_KIND:
        raise ValueError("weights.pt requires a model weights artifact")


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


def load_training_checkpoint(path: Path) -> dict[str, object]:
    """安全なweights-only modeでtraining checkpointを読む."""

    payload = torch.load(path, map_location="cpu", weights_only=True)
    if not isinstance(payload, dict):
        raise ValueError("training checkpoint must be a mapping")
    _validate_torch_artifact(payload)
    if payload.get("kind") != CHECKPOINT_KIND:
        raise ValueError("invalid training checkpoint kind")
    return cast(dict[str, object], payload)


def load_model_weights(path: Path) -> dict[str, object]:
    """安全なweights-only modeでstrict v1 model weightsを読む."""

    payload = torch.load(path, map_location="cpu", weights_only=True)
    if not isinstance(payload, dict):
        raise ValueError("model weights must be a mapping")
    _validate_torch_artifact(payload)
    if payload.get("kind") != WEIGHT_KIND:
        raise ValueError("invalid model weights kind")
    return cast(dict[str, object], payload)


def load_formal_training_weights(
    weights_path: Path,
    *,
    tracking_uri: str,
) -> FormalTrainingWeights:
    """正式な学習run、remote attestation、strict weightsを一体で検証する."""

    candidate = Path(weights_path).expanduser()
    if candidate.is_symlink():
        raise ValueError(f"formal training weights must not be a symlink: {candidate}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ValueError(
            f"formal training weights do not exist: {candidate}"
        ) from error
    if not resolved.is_file():
        raise ValueError(f"formal training weights must be a regular file: {resolved}")

    payload = load_model_weights(resolved)
    run_kind = cast(RunKind, payload["run_kind"])
    attestation = verify_formal_artifact(
        resolved,
        tracking_uri=tracking_uri,
        expected_run_kind=run_kind,
    )
    source_run_id = cast(str, payload["source_run_id"])
    if attestation.run_id != source_run_id:
        raise ValueError(
            "formal training weights source_run_id does not match attestation"
        )
    weights_sha256 = _file_sha256(resolved)
    if attestation.output_fingerprint != weights_sha256:
        raise ValueError("formal training weights hash does not match attestation")

    from mlflow import MlflowClient

    run = MlflowClient(tracking_uri=tracking_uri).get_run(attestation.run_id)
    tags = run.data.tags
    expected_tags = {
        "run_kind": run_kind,
        "dataset_fingerprint": cast(str, payload["dataset_fingerprint"]),
        "split_fingerprint": cast(str, payload["split_fingerprint"]),
        "training_protocol_fingerprint": cast(
            str, payload["training_protocol_fingerprint"]
        ),
    }
    for name, expected in expected_tags.items():
        if tags.get(name) != expected:
            raise ValueError(
                f"formal training weights MLflow tag does not match: {name}"
            )
    if any(name.startswith("hpo.") for name in tags):
        raise ValueError("HPO trial weights cannot be used as a formal export source")

    return FormalTrainingWeights(
        weights_path=resolved,
        weights_sha256=weights_sha256,
        run_kind=run_kind,
        source_run_id=source_run_id,
        dataset_fingerprint=expected_tags["dataset_fingerprint"],
        split_fingerprint=expected_tags["split_fingerprint"],
        training_protocol_fingerprint=expected_tags["training_protocol_fingerprint"],
        attestation=attestation,
    )


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
        "kind": CHECKPOINT_KIND,
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
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
        RegressionMetrics(**dict(metrics_raw)) if metrics_raw is not None else None
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
    formal_weights: FormalTrainingWeights | None = None
    if formal_tracking_uri is not None:
        formal_weights = load_formal_training_weights(
            path,
            tracking_uri=formal_tracking_uri,
        )
        if formal_weights.run_kind != "base-train":
            raise ValueError(
                "formal finetune initial weights must come from a base-train run"
            )
    payload = load_model_weights(path)
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


def _prediction_metrics(
    mean: Tensor,
    log_variance: Tensor,
    target: Tensor,
    weights: Tensor,
) -> RegressionMetrics:
    mean = mean.detach().double().flatten().cpu()
    log_variance = log_variance.detach().double().flatten().cpu()
    target = target.detach().double().flatten().cpu()
    weights = weights.detach().double().flatten().cpu()
    finite = (
        torch.isfinite(mean)
        & torch.isfinite(log_variance)
        & torch.isfinite(target)
        & torch.isfinite(weights)
        & (mean > 0)
        & (target > 0)
        & (weights >= 0)
    )
    invalid_count = int((~finite).sum().item())
    if invalid_count or not finite.any().item() or weights[finite].sum().item() <= 0:
        raise ValueError("predictions, targets, and weights must be valid and finite")
    error = mean - target
    weight_sum = weights.sum()
    nll = (
        0.5 * (torch.exp(-log_variance) * error.square() + log_variance) * weights
    ).sum() / weight_sum
    mae = (error.abs() * weights).sum() / weight_sum
    rmse = torch.sqrt((error.square() * weights).sum() / weight_sum)
    std = torch.sqrt(torch.exp(log_variance))
    normalized_error = error / target
    normalized_mean = (normalized_error * weights).sum() / weight_sum
    normalized_variance = (
        normalized_error.sub(normalized_mean).square() * weights
    ).sum() / weight_sum
    normalized_std = torch.sqrt(normalized_variance)
    relative = error.abs() / target
    coverage = ((error.abs() <= std).double() * weights).sum() / weight_sum
    return RegressionMetrics(
        gaussian_nll=float(nll.item()),
        mae_ul=float(mae.item()),
        rmse_ul=float(rmse.item()),
        normalized_error_mean=float(normalized_mean.item()),
        normalized_error_std=float(normalized_std.item()),
        normalized_error_score=float((normalized_mean.abs() + normalized_std).item()),
        median_absolute_relative_error=float(torch.quantile(relative, 0.5).item()),
        p95_absolute_relative_error=float(torch.quantile(relative, 0.95).item()),
        one_std_coverage=float(coverage.item()),
        mean_prediction_std_ul=float((std * weights).sum().div(weight_sum).item()),
        invalid_prediction_count=invalid_count,
        sample_count=int(target.numel()),
    )


@dataclass(frozen=True)
class EvaluationPredictions:
    metrics: RegressionMetrics
    mean_volume_ul: Tensor
    log_variance_volume_ul2: Tensor
    target_volume_ul: Tensor
    sample_weight: Tensor
    sample_ids: tuple[str, ...]


def evaluate_batches(
    model_forward: Callable[[Tensor, Tensor, Tensor], tuple[Tensor, Tensor]],
    data: TrainingData,
    *,
    split: Literal["validation", "test"],
    device: torch.device,
    log_variance_offset: float = 0.0,
    deadline_monotonic: float | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> EvaluationPredictions:
    """指定splitを一度評価し、metricと全predictionを返す.

    ``stop_requested`` はbatch境界で評価を止める。実行中のbatchで
    requestが立った場合も、次のbatchはmaterializeしない。
    """

    def ensure_running() -> None:
        if stop_requested is not None and stop_requested():
            raise _EvaluationStopRequested("evaluation stop requested")
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise TimeoutError("training finalization deadline exceeded")

    means: list[Tensor] = []
    log_variances: list[Tensor] = []
    targets: list[Tensor] = []
    weights: list[Tensor] = []
    sample_ids: list[str] = []
    with torch.inference_mode():
        for batch_ids in data.evaluation_batch_plan(split):
            ensure_running()
            batch = data.evaluation_batch(batch_ids, split=split).to(device)
            ensure_running()
            validate_model_inputs(
                batch.image_6ch, batch.valid_pixel_mask, batch.pixel_per_mm
            )
            mean, log_variance = model_forward(
                batch.image_6ch, batch.valid_pixel_mask, batch.pixel_per_mm
            )
            ensure_running()
            adjusted = log_variance + log_variance_offset
            means.append(mean.cpu())
            log_variances.append(adjusted.cpu())
            targets.append(batch.target_volume_ul.cpu())
            weights.append(batch.sample_weight.cpu())
            sample_ids.extend(batch.sample_ids)
    if not means:
        raise ValueError(f"{split} split has no batches")
    all_mean = torch.cat(means)
    all_log_variance = torch.cat(log_variances)
    all_targets = torch.cat(targets)
    all_weights = torch.cat(weights)
    metrics = _prediction_metrics(all_mean, all_log_variance, all_targets, all_weights)
    return EvaluationPredictions(
        metrics=metrics,
        mean_volume_ul=all_mean,
        log_variance_volume_ul2=all_log_variance,
        target_volume_ul=all_targets,
        sample_weight=all_weights,
        sample_ids=tuple(sample_ids),
    )


def fit_log_variance_offset(predictions: EvaluationPredictions) -> float:
    """ValidationだけからGaussian NLL最適なscalar log-variance offsetを求める."""

    error_squared = torch.square(
        predictions.target_volume_ul.double() - predictions.mean_volume_ul.double()
    )
    scaled = torch.exp(-predictions.log_variance_volume_ul2.double()) * error_squared
    weights = predictions.sample_weight.double()
    optimum = torch.sum(scaled * weights) / torch.sum(weights)
    if not torch.isfinite(optimum).item() or optimum.item() <= 0:
        raise ValueError("validation predictions cannot calibrate uncertainty")
    return float(torch.log(optimum).item())


def extract_best_weights(
    best_checkpoint: Path,
    output_path: Path,
    *,
    uncertainty_log_variance_offset: float,
) -> Path:
    """厳格にbest roleのcheckpointだけを推論用weightsへ変換する."""

    checkpoint = load_training_checkpoint(best_checkpoint)
    if checkpoint.get("checkpoint_role") != "best":
        raise ValueError("weights can only be extracted from a best checkpoint")
    payload: dict[str, object] = {
        "weight_schema_version": WEIGHT_SCHEMA_VERSION,
        "kind": WEIGHT_KIND,
        "model_config": checkpoint["model_config"],
        "state_dict": checkpoint["model_state"],
        "preprocess_schema": checkpoint["preprocess_schema"],
        "uncertainty_log_variance_offset": float(uncertainty_log_variance_offset),
        "source_run_id": checkpoint["run_id"],
        "source_checkpoint_role": "best",
        "source_checkpoint_sha256": _file_sha256(best_checkpoint),
        "dataset_fingerprint": checkpoint["dataset_fingerprint"],
        "split_fingerprint": checkpoint["split_fingerprint"],
        "training_protocol_fingerprint": checkpoint["training_protocol_fingerprint"],
        "run_kind": checkpoint["run_kind"],
        "parent_run_id": checkpoint.get("parent_run_id"),
        "parent_checkpoint_id": checkpoint.get("parent_checkpoint_id"),
    }
    atomic_torch_save(payload, output_path)
    return output_path


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
    atomic_torch_save(
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
    metrics: RegressionMetrics,
    state: _TrainState,
    min_delta: float,
) -> bool:
    if metrics.gaussian_nll < state.best_validation_nll - min_delta:
        return True
    return (
        abs(metrics.gaussian_nll - state.best_validation_nll) <= min_delta
        and metrics.mae_ul < state.best_validation_mae
    )


def _all_gradients_finite(model: nn.Module) -> bool:
    return all(
        torch.all(torch.isfinite(parameter.grad)).item()
        for parameter in model.parameters()
        if parameter.grad is not None
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
    data: TrainingData,
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
    seed_everything(config.trainer.seed, deterministic=config.trainer.deterministic)
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
        resume_payload = load_training_checkpoint(config.checkpoint.resume_checkpoint)
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
    optimizer_update_in_progress = False

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
    ) -> EvaluationPredictions:
        try:
            return evaluate_batches(
                evaluated_model,
                data,
                split="validation",
                device=device,
                log_variance_offset=log_variance_offset,
                deadline_monotonic=finalization_deadline,
                stop_requested=lambda: termination.requested,
            )
        except _EvaluationStopRequested:
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
            if not _all_gradients_finite(model):
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
                if not _all_gradients_finite(model):
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
        last_validation_metrics: RegressionMetrics | None = None
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
                epoch_means: list[Tensor] = []
                epoch_log_variances: list[Tensor] = []
                epoch_targets: list[Tensor] = []
                epoch_weights: list[Tensor] = []
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
                    group_started_at = time.monotonic()
                    group_rng = _rng_state()
                    active_group_start = group_start
                    active_group_rng = group_rng
                    optimizer_update_in_progress = False
                    group_size = min(
                        config.trainer.gradient_accumulation_steps,
                        len(plan) - group_start,
                    )
                    optimizer.zero_grad(set_to_none=True)
                    group_failed = False
                    group_deadline_expired = False
                    group_metric_start = len(epoch_means)
                    group_sample_start = epoch_sample_count
                    for _ in range(group_size):
                        if deadline is not None and time.monotonic() >= deadline:
                            group_deadline_expired = True
                            break
                        batch_ids = plan[batch_index]
                        batch = data.training_batch(batch_ids, epoch=state.epoch).to(
                            device
                        )
                        validate_model_inputs(
                            batch.image_6ch,
                            batch.valid_pixel_mask,
                            batch.pixel_per_mm,
                        )
                        with torch.autocast(
                            device_type=device.type,
                            enabled=device.type == "cuda",
                        ):
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
                            scaled_loss = loss / group_size
                        distinct_batch_shapes.add(
                            (
                                int(batch.image_6ch.shape[0]),
                                int(batch.image_6ch.shape[2]),
                                int(batch.image_6ch.shape[3]),
                            )
                        )
                        epoch_means.append(mean.detach().cpu())
                        epoch_log_variances.append(log_variance.detach().cpu())
                        epoch_targets.append(batch.target_volume_ul.detach().cpu())
                        epoch_weights.append(batch.sample_weight.detach().cpu())
                        if not torch.isfinite(loss).item():
                            group_failed = True
                            break
                        scaler.scale(scaled_loss).backward()
                        epoch_sample_count += len(batch.sample_ids)
                        batch_index += 1
                    if deadline is not None and time.monotonic() >= deadline:
                        group_deadline_expired = True
                    if group_deadline_expired:
                        optimizer.zero_grad(set_to_none=True)
                        _restore_rng_state(group_rng)
                        del epoch_means[group_metric_start:]
                        del epoch_log_variances[group_metric_start:]
                        del epoch_targets[group_metric_start:]
                        del epoch_weights[group_metric_start:]
                        epoch_sample_count = group_sample_start
                        batch_index = group_start
                        state.next_batch_index = group_start
                        active_group_start = None
                        active_group_rng = None
                        stopped_reason = "deadline"
                        epoch_was_interrupted = True
                        break
                    if not group_failed:
                        scaler.unscale_(optimizer)
                        group_failed = not _all_gradients_finite(model)
                    if group_failed:
                        optimizer.zero_grad(set_to_none=True)
                        state.next_batch_index = group_start
                        raise FloatingPointError(
                            "non-finite loss or gradient; accumulation group rolled back"
                        )
                    torch.nn.utils.clip_grad_norm_(
                        model.parameters(), config.trainer.gradient_clip_norm
                    )
                    optimizer_update_in_progress = True
                    scaler.step(optimizer)
                    scaler.update()
                    group_elapsed = time.monotonic() - group_started_at
                    if first_optimizer_step_seconds is None:
                        first_optimizer_step_seconds = group_elapsed
                    else:
                        steady_state_samples += sum(
                            len(plan[index])
                            for index in range(group_start, batch_index)
                        )
                        steady_state_seconds += group_elapsed
                    state.global_step += 1
                    state.next_batch_index = batch_index
                    optimizer_update_in_progress = False
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
                train_metrics = (
                    _prediction_metrics(
                        torch.cat(epoch_means),
                        torch.cat(epoch_log_variances),
                        torch.cat(epoch_targets),
                        torch.cat(epoch_weights),
                    )
                    if epoch_means
                    else None
                )
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
        best_payload = load_training_checkpoint(best_path)
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
        offset = fit_log_variance_offset(validation_uncalibrated)
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
            calibrated_payload = load_training_checkpoint(calibrated_checkpoint)
            calibrated_payload["uncertainty_log_variance_offset"] = offset
            atomic_torch_save(calibrated_payload, calibrated_checkpoint)
        extract_best_weights(
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
                if optimizer_update_in_progress:
                    # An optimizer update is not assumed atomic.  Fall back to the
                    # most recent fully committed checkpoint rather than persisting
                    # a possibly half-updated parameter/optimizer pair.
                    safe_payload = load_training_checkpoint(latest_path)
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
    "CHECKPOINT_KIND",
    "CHECKPOINT_SCHEMA_VERSION",
    "CheckpointConfig",
    "EvaluationPredictions",
    "FormalTrainingWeights",
    "RegressionMetrics",
    "TrainResult",
    "TrainerConfig",
    "TrainingBatch",
    "TrainingCoreConfig",
    "TrainingData",
    "TrainingDeadlineExceeded",
    "TrainingInterrupted",
    "WEIGHT_KIND",
    "WEIGHT_SCHEMA_VERSION",
    "atomic_torch_save",
    "evaluate_batches",
    "extract_best_weights",
    "fit_log_variance_offset",
    "load_model_weights",
    "load_formal_training_weights",
    "load_training_checkpoint",
    "seed_everything",
    "train_model",
]
