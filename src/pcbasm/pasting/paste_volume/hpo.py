"""Persistent Optuna studyのidentity、storage、結果artifact契約."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def search_config_fingerprint(search_config: Mapping[str, object]) -> str:
    """pathやmapping順序へ依存しないsearch設定fingerprintを返す."""

    return "sha256:" + hashlib.sha256(_canonical_json(search_config)).hexdigest()


def build_optuna_study_name(
    *,
    model_family: str,
    dataset_fingerprint: str,
    search_config: Mapping[str, object],
) -> str:
    """Model/dataset/search identityから安定study名を作る."""

    if not model_family or not dataset_fingerprint:
        raise ValueError("model family and dataset fingerprint are required")
    safe_family = re.sub(r"[^a-zA-Z0-9_.-]+", "-", model_family).strip("-")
    dataset_hash = dataset_fingerprint.removeprefix("sha256:")[:12]
    search_hash = search_config_fingerprint(search_config).removeprefix("sha256:")[:12]
    return f"{safe_family}-{dataset_hash}-{search_hash}"


def validate_optuna_storage(storage_uri: str) -> None:
    """永続storageだけを許可し、SQLiteは絶対pathを要求する."""

    if not storage_uri:
        raise ValueError("Optuna storage URI is required")
    if storage_uri.startswith("sqlite:///"):
        database_path = Path(storage_uri.removeprefix("sqlite:///"))
        if not database_path.is_absolute():
            raise ValueError("Optuna SQLite storage must use an absolute path")
        return
    parsed = urlsplit(storage_uri)
    if parsed.scheme not in ("postgresql", "postgresql+psycopg", "mysql"):
        raise ValueError("Optuna storage must be persistent SQLite/PostgreSQL/MySQL")
    if not parsed.hostname or not parsed.path.strip("/"):
        raise ValueError("Optuna server storage requires host and database")


def redact_storage_uri(storage_uri: str) -> str:
    """Artifact/tagへ残せるcredential-free storage identityを返す."""

    validate_optuna_storage(storage_uri)
    parsed = urlsplit(storage_uri)
    if storage_uri.startswith("sqlite:///"):
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def write_optimization_results(
    path: Path,
    *,
    study_name: str,
    storage_uri: str,
    direction: str,
    trials: Sequence[Mapping[str, object]],
) -> Path:
    """全trialを含むcredential-free YAMLをatomic保存する."""

    payload = {
        "kind": "pcbasm-paste-volume-optuna-results",
        "schema_version": 1,
        "study_name": study_name,
        "storage_uri": redact_storage_uri(storage_uri),
        "direction": direction,
        "trials": [dict(trial) for trial in trials],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        yaml.safe_dump(payload, stream, allow_unicode=True, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return path


def optuna_trials_payload(
    study: Any,
    *,
    run_ids_by_trial: Mapping[int, str] | None = None,
) -> tuple[dict[str, object], ...]:
    """Optuna FrozenTrial群をstable artifact payloadへ変換する."""

    run_ids = run_ids_by_trial or {}
    result: list[dict[str, object]] = []
    for trial in sorted(study.get_trials(deepcopy=False), key=lambda item: item.number):
        result.append(
            {
                "number": int(trial.number),
                "state": str(trial.state.name),
                "value": float(trial.value) if trial.value is not None else None,
                "params": dict(sorted(trial.params.items())),
                "mlflow_run_id": run_ids.get(int(trial.number)),
            }
        )
    return tuple(result)


def build_trial_metadata_overrides(
    *,
    study_name: str,
    trial_number: int,
    search_fingerprint: str,
    storage_uri: str,
    checkpoint_directory: Path,
) -> tuple[str, ...]:
    """Hydra jobへstudy/trial lineageを注入するoverride群を返す."""

    if trial_number < 0:
        raise ValueError("trial_number must be non-negative")
    redacted = redact_storage_uri(storage_uri)
    return (
        f"hpo.study_name={json.dumps(study_name)}",
        f"hpo.trial_number={trial_number}",
        "hpo.search_config_fingerprint=" + json.dumps(search_fingerprint),
        "hpo.storage_uri_redacted=" + json.dumps(redacted),
        "checkpoint.directory=" + json.dumps(str(checkpoint_directory.resolve())),
        "logger.run_name=" + json.dumps(f"{study_name}-trial-{trial_number}"),
    )


__all__ = [
    "build_optuna_study_name",
    "build_trial_metadata_overrides",
    "optuna_trials_payload",
    "redact_storage_uri",
    "search_config_fingerprint",
    "validate_optuna_storage",
    "write_optimization_results",
]
