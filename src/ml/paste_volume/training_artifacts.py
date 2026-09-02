"""Paste-volume checkpoint/weights schema v1とcodec."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from ml.training.checkpoint import (
    atomic_torch_save as _atomic_torch_save,
    load_torch_mapping,
)

from .formal_artifact import FormalArtifactAttestation, verify_formal_artifact

CHECKPOINT_KIND = "pcbasm-paste-volume-training-checkpoint"
CHECKPOINT_SCHEMA_VERSION = 1
WEIGHT_KIND = "paste-volume-model-weights"
WEIGHT_SCHEMA_VERSION = 1
_CHECKPOINT_ROLES = frozenset({"latest", "best", "final", "emergency"})
_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")

type RunKind = Literal["base-train", "finetune"]


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


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def atomic_torch_save(payload: Mapping[str, object], path: Path) -> None:
    """同一directoryの一時fileを検証・fsync後、atomic replaceする."""

    _atomic_torch_save(
        payload,
        path,
        validate=_validate_torch_artifact,
        validate_readback=_validate_torch_artifact_readback,
    )


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


def _validate_torch_artifact_readback(
    expected: Mapping[str, object],
    loaded: Mapping[str, object],
    path: Path,
) -> None:
    _validate_torch_artifact_identity(expected, loaded)
    _validate_torch_artifact_destination(loaded, path)


def load_training_checkpoint(path: Path) -> dict[str, object]:
    """安全なweights-only modeでtraining checkpointを読む."""

    payload = load_torch_mapping(path, description="training checkpoint")
    _validate_torch_artifact(payload)
    if payload.get("kind") != CHECKPOINT_KIND:
        raise ValueError("invalid training checkpoint kind")
    return cast(dict[str, object], payload)


def load_model_weights(path: Path) -> dict[str, object]:
    """安全なweights-only modeでstrict v1 model weightsを読む."""

    payload = load_torch_mapping(path, description="model weights")
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
    weights_sha256 = file_sha256(resolved)
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
        "source_checkpoint_sha256": file_sha256(best_checkpoint),
        "dataset_fingerprint": checkpoint["dataset_fingerprint"],
        "split_fingerprint": checkpoint["split_fingerprint"],
        "training_protocol_fingerprint": checkpoint["training_protocol_fingerprint"],
        "run_kind": checkpoint["run_kind"],
        "parent_run_id": checkpoint.get("parent_run_id"),
        "parent_checkpoint_id": checkpoint.get("parent_checkpoint_id"),
    }
    atomic_torch_save(payload, output_path)
    return output_path


__all__ = [
    "CHECKPOINT_KIND",
    "CHECKPOINT_SCHEMA_VERSION",
    "FormalTrainingWeights",
    "RunKind",
    "WEIGHT_KIND",
    "WEIGHT_SCHEMA_VERSION",
    "atomic_torch_save",
    "extract_best_weights",
    "file_sha256",
    "load_formal_training_weights",
    "load_model_weights",
    "load_training_checkpoint",
]
