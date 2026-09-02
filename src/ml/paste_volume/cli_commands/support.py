"""Paste-volume tracking adapter for generic command operations."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ml.cli.provenance import sanitize_persisted_text
from ml.cli.runner import jsonable
from ml.cli.tracked_operation import (
    OperationFinalizer,
    OperationLogger,
    OperationOutput,
    require_tracking_server_uri,
    run_tracked_operation,
)


def require_formal_artifact(
    artifact: Path,
    *,
    tracking_uri: str,
    expected_run_kind: str | None = None,
) -> None:
    from ml.paste_volume.formal_artifact import verify_formal_artifact

    verify_formal_artifact(
        artifact,
        tracking_uri=tracking_uri,
        expected_run_kind=expected_run_kind,
    )


def tracked_operation(
    args: argparse.Namespace,
    *,
    run_kind: str,
    action: Callable[[], OperationOutput],
    initial_tags: Mapping[str, str | int | float | bool] | None = None,
) -> Any:
    from ml.artifacts.formal import formal_artifact_attestation_path
    from ml.paste_volume.formal_artifact import (
        publish_formal_artifact,
    )
    from ml.training.experiment import MLflowExperimentLogger

    tracking_uri = require_tracking_server_uri(str(args.tracking_uri))
    experiment_name = sanitize_persisted_text(str(args.experiment_name))
    logger = MLflowExperimentLogger(
        tracking_uri=tracking_uri,
        experiment_name=experiment_name,
    )
    formal_output = Path(args.output).expanduser().resolve()

    def finalize(
        output: Path,
        *,
        tracking_uri: str,
        run_kind: str,
        logger: OperationLogger,
    ) -> object:
        return publish_formal_artifact(
            output,
            tracking_uri=tracking_uri,
            run_kind=run_kind,
            logger=logger,
        )

    finalizer: OperationFinalizer = finalize
    return run_tracked_operation(
        args,
        run_kind=run_kind,
        action=action,
        logger=logger,
        finalizer=finalizer,
        output=formal_output,
        completion_marker=formal_artifact_attestation_path(formal_output),
        initial_tags=initial_tags,
    )


def lineage_tags(lineage: Any) -> dict[str, str]:
    tags = {
        "source_run_id": str(lineage.source_run_id),
        "source_checkpoint_sha256": str(lineage.source_checkpoint_sha256),
        "dataset_fingerprint": str(lineage.dataset_fingerprint),
        "split_fingerprint": str(lineage.split_fingerprint),
    }
    if lineage.parent_run_id is not None:
        tags["parent_run_id"] = str(lineage.parent_run_id)
    if lineage.parent_checkpoint_id is not None:
        tags["parent_checkpoint_id"] = str(lineage.parent_checkpoint_id)
    return tags


def write_report(path: Path, payload: Any) -> Path:
    if path.exists():
        raise FileExistsError(f"operation report already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
        json.dump(jsonable(payload), stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return path


def load_formal_training_weights(path: str, *, tracking_uri: str) -> Any:
    from ml.paste_volume.training_artifacts import load_formal_training_weights

    return load_formal_training_weights(
        Path(path).expanduser().resolve(strict=True),
        tracking_uri=tracking_uri,
    )
