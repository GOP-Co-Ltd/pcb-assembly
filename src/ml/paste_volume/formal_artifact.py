"""Paste-volume formal artifactのschema specialization."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from ml.artifacts.formal import (
    publish_formal_artifact as _publish_formal_artifact,
    verify_formal_artifact as _verify_formal_artifact,
)
from ml.training.experiment import ExperimentLogger

FORMAL_SUCCESS_KIND = "pcbasm-paste-volume-formal-operation-success"
FORMAL_SUCCESS_SCHEMA_VERSION = 1
FORMAL_SUCCESS_ARTIFACT = "operation/formal-success.json"

_ATTESTATION_KEYS = frozenset(
    {
        "kind",
        "schema_version",
        "status",
        "run_id",
        "run_kind",
        "tracking_uri_sha256",
        "output_path",
        "output_kind",
        "output_fingerprint",
    }
)
_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True, kw_only=True)
class FormalArtifactAttestation:
    """1個の不変なlocal出力を完了済みMLflow runへ結び付けるschema."""

    run_id: str
    run_kind: str
    tracking_uri_sha256: str
    output_path: Path
    output_kind: Literal["file", "directory"]
    output_fingerprint: str
    kind: str = FORMAL_SUCCESS_KIND
    schema_version: int = FORMAL_SUCCESS_SCHEMA_VERSION
    status: str = "FINISHED"

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind != FORMAL_SUCCESS_KIND:
            raise ValueError("formal artifact attestation kind is invalid")
        if (
            type(self.schema_version) is not int
            or self.schema_version != FORMAL_SUCCESS_SCHEMA_VERSION
        ):
            raise ValueError("formal artifact attestation schema_version is invalid")
        if not isinstance(self.status, str) or self.status != "FINISHED":
            raise ValueError("formal artifact attestation status must be FINISHED")
        for name, value in (("run_id", self.run_id), ("run_kind", self.run_kind)):
            if not isinstance(value, str) or not value or value.strip() != value:
                raise ValueError(f"formal artifact attestation {name} is invalid")
        for name, value in (
            ("tracking_uri_sha256", self.tracking_uri_sha256),
            ("output_fingerprint", self.output_fingerprint),
        ):
            if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
                raise ValueError(f"formal artifact attestation {name} is invalid")
        if not isinstance(self.output_path, Path) or not self.output_path.is_absolute():
            raise ValueError("formal artifact attestation output_path must be absolute")
        if self.output_kind not in ("file", "directory"):
            raise ValueError("formal artifact attestation output_kind is invalid")

    def to_dict(self) -> dict[str, object]:
        """Canonical JSON表現を返す."""

        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "status": self.status,
            "run_id": self.run_id,
            "run_kind": self.run_kind,
            "tracking_uri_sha256": self.tracking_uri_sha256,
            "output_path": str(self.output_path),
            "output_kind": self.output_kind,
            "output_fingerprint": self.output_fingerprint,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> FormalArtifactAttestation:
        """missing、extra、型不一致を拒否してattestationを読む."""

        if set(value) != _ATTESTATION_KEYS:
            raise ValueError("formal artifact attestation key set is invalid")
        schema_version = value["schema_version"]
        if type(schema_version) is not int:
            raise ValueError("formal artifact attestation schema_version is invalid")
        strings: dict[str, str] = {}
        for name in (
            "kind",
            "status",
            "run_id",
            "run_kind",
            "tracking_uri_sha256",
            "output_path",
            "output_kind",
            "output_fingerprint",
        ):
            item = value[name]
            if not isinstance(item, str):
                raise ValueError(f"formal artifact attestation {name} is invalid")
            strings[name] = item
        output_kind = strings["output_kind"]
        if output_kind not in ("file", "directory"):
            raise ValueError("formal artifact attestation output_kind is invalid")
        return cls(
            kind=strings["kind"],
            schema_version=schema_version,
            status=strings["status"],
            run_id=strings["run_id"],
            run_kind=strings["run_kind"],
            tracking_uri_sha256=strings["tracking_uri_sha256"],
            output_path=Path(strings["output_path"]),
            output_kind=cast(Literal["file", "directory"], output_kind),
            output_fingerprint=strings["output_fingerprint"],
        )


def _create_attestation(
    run_id: str,
    run_kind: str,
    tracking_uri_sha256: str,
    output_path: Path,
    output_kind: Literal["file", "directory"],
    output_fingerprint: str,
) -> FormalArtifactAttestation:
    return FormalArtifactAttestation(
        run_id=run_id,
        run_kind=run_kind,
        tracking_uri_sha256=tracking_uri_sha256,
        output_path=output_path,
        output_kind=output_kind,
        output_fingerprint=output_fingerprint,
    )


def publish_formal_artifact(
    output: Path,
    *,
    tracking_uri: str,
    run_kind: str,
    logger: ExperimentLogger,
) -> FormalArtifactAttestation:
    """完了済みrunとpaste-volume出力のlocal attestationを発行する."""

    return _publish_formal_artifact(
        output,
        tracking_uri=tracking_uri,
        run_kind=run_kind,
        logger=logger,
        create_attestation=_create_attestation,
        remote_artifact_path=FORMAL_SUCCESS_ARTIFACT,
    )


def verify_formal_artifact(
    artifact: Path,
    *,
    tracking_uri: str,
    expected_run_kind: str | None = None,
) -> FormalArtifactAttestation:
    """Local/remoteのpaste-volume formal evidenceを検証する."""

    return _verify_formal_artifact(
        artifact,
        tracking_uri=tracking_uri,
        parse_attestation=FormalArtifactAttestation.from_dict,
        remote_artifact_path=FORMAL_SUCCESS_ARTIFACT,
        expected_run_kind=expected_run_kind,
    )


__all__ = [
    "FORMAL_SUCCESS_ARTIFACT",
    "FORMAL_SUCCESS_KIND",
    "FORMAL_SUCCESS_SCHEMA_VERSION",
    "FormalArtifactAttestation",
    "publish_formal_artifact",
    "verify_formal_artifact",
]
