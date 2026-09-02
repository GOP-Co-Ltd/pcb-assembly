"""Formal MLflow artifact attestation producer and verifier.

The local sibling is deliberately published only after the corresponding MLflow
run has reached ``FINISHED``.  A consumer must still verify the live output, the
remote run, its tags, and the remote copy of the attestation before trusting it.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal, Protocol, TypeVar, cast

from ml.training.experiment import ExperimentLogger, write_json_artifact

type OutputKind = Literal["file", "directory"]


class FormalArtifactAttestation(Protocol):
    """Formal artifact mechanicsが必要とするattestation契約."""

    @property
    def run_id(self) -> str: ...

    @property
    def run_kind(self) -> str: ...

    @property
    def tracking_uri_sha256(self) -> str: ...

    @property
    def output_path(self) -> Path: ...

    @property
    def output_kind(self) -> OutputKind: ...

    @property
    def output_fingerprint(self) -> str: ...

    @property
    def status(self) -> str: ...

    def to_dict(self) -> dict[str, object]: ...


AttestationT = TypeVar("AttestationT", bound=FormalArtifactAttestation)
type AttestationFactory[AttestationT: FormalArtifactAttestation] = Callable[
    [str, str, str, Path, OutputKind, str], AttestationT
]
type AttestationParser[AttestationT: FormalArtifactAttestation] = Callable[
    [Mapping[str, object]], AttestationT
]


def formal_artifact_attestation_path(output: Path) -> Path:
    """Return the canonical create-only sibling path for ``output``."""

    resolved = Path(output).expanduser().resolve()
    return resolved.with_name(f"{resolved.name}.mlflow-success.json")


def publish_formal_artifact(
    output: Path,
    *,
    tracking_uri: str,
    run_kind: str,
    logger: ExperimentLogger,
    create_attestation: AttestationFactory[AttestationT],
    remote_artifact_path: str,
) -> AttestationT:
    """Finish an active run and publish its local success attestation.

    The caller must have started ``logger`` and completed all ordinary logging.
    This function owns the terminal logger transition.  Failures before a
    successful ``FINISHED`` transition best-effort close the run as ``FAILED``;
    no local attestation is published on any failure.
    """

    finished = False
    try:
        if not tracking_uri:
            raise ValueError("tracking_uri is required")
        if not run_kind or run_kind.strip() != run_kind:
            raise ValueError("run_kind is invalid")
        root = _resolve_formal_output(output)
        local_path = formal_artifact_attestation_path(root)
        if os.path.lexists(local_path):
            raise FileExistsError(
                f"formal success attestation already exists: {local_path}"
            )
        output_fingerprint = _path_fingerprint(root)
        remote_directory, remote_name = _split_remote_artifact_path(
            remote_artifact_path
        )
        attestation = create_attestation(
            logger.run_id,
            run_kind,
            _sha256_text(tracking_uri),
            root,
            "directory" if root.is_dir() else "file",
            output_fingerprint,
        )
        logger.set_tags(
            {
                "formal_output_path": str(root),
                "formal_output_fingerprint": output_fingerprint,
                "formal_operation_run_kind": run_kind,
            }
        )
        with tempfile.TemporaryDirectory(
            prefix="ml-formal-attestation-write-"
        ) as directory:
            remote_path = write_json_artifact(
                Path(directory) / remote_name, attestation.to_dict()
            )
            logger.log_artifact(remote_path, artifact_path=remote_directory)
        logger.flush()
        logger.end(status="FINISHED")
        finished = True
    except Exception:
        if not finished:
            _end_failed(logger)
        raise

    if _path_fingerprint(root) != attestation.output_fingerprint:
        raise RuntimeError(
            "formal output changed while the MLflow run was being finalized; "
            "local attestation was not published"
        )
    _write_json_create_only(local_path, attestation.to_dict())
    return attestation


def verify_formal_artifact(
    artifact: Path,
    *,
    tracking_uri: str,
    parse_attestation: AttestationParser[AttestationT],
    remote_artifact_path: str,
    expected_run_kind: str | None = None,
) -> AttestationT:
    """Verify local and remote evidence for a formally attested artifact."""

    if not tracking_uri:
        raise ValueError("tracking_uri is required")
    root, local_path = _find_formal_output_root(artifact)
    attestation = parse_attestation(
        _read_json_object(local_path, description="formal MLflow success attestation")
    )
    if expected_run_kind is not None and attestation.run_kind != expected_run_kind:
        raise ValueError("formal MLflow success attestation run_kind is invalid")
    expected_kind: OutputKind = "directory" if root.is_dir() else "file"
    if (
        attestation.tracking_uri_sha256 != _sha256_text(tracking_uri)
        or attestation.output_path != root
        or attestation.output_kind != expected_kind
        or attestation.output_fingerprint != _path_fingerprint(root)
    ):
        raise ValueError("formal MLflow success attestation does not match artifact")

    from mlflow import MlflowClient

    client = MlflowClient(tracking_uri=tracking_uri)
    run = client.get_run(attestation.run_id)
    if run.info.status != "FINISHED":
        raise ValueError(f"formal MLflow run is not FINISHED: {attestation.run_id}")
    tags = run.data.tags
    if (
        tags.get("formal_output_path") != str(root)
        or tags.get("formal_output_fingerprint") != attestation.output_fingerprint
        or tags.get("formal_operation_run_kind") != attestation.run_kind
    ):
        raise ValueError("formal MLflow run tags do not match attestation")
    with tempfile.TemporaryDirectory(prefix="ml-formal-attestation-read-") as directory:
        downloaded = Path(
            client.download_artifacts(
                attestation.run_id,
                remote_artifact_path,
                directory,
            )
        )
        remote = parse_attestation(
            _read_json_object(
                downloaded,
                description="remote formal MLflow success attestation",
            )
        )
    if remote != attestation:
        raise ValueError("local and remote formal MLflow attestations do not match")
    return attestation


def _split_remote_artifact_path(value: str) -> tuple[str | None, str]:
    path = Path(value)
    if (
        not value
        or path.is_absolute()
        or path.name in ("", ".", "..")
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise ValueError("remote formal artifact path is invalid")
    parent = path.parent.as_posix()
    return (None if parent == "." else parent), path.name


def _resolve_formal_output(path: Path) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise ValueError(f"formal output must not be a symlink: {candidate}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"formal output does not exist: {candidate}") from error
    mode = resolved.stat(follow_symlinks=False).st_mode
    if not stat.S_ISREG(mode) and not stat.S_ISDIR(mode):
        raise ValueError(
            f"formal output must be a regular file or directory: {resolved}"
        )
    return resolved


def _path_fingerprint(path: Path) -> str:
    resolved = _resolve_formal_output(path)
    if resolved.is_file():
        return _sha256_file(resolved)

    files: list[Path] = []
    for directory, directory_names, file_names in os.walk(resolved, followlinks=False):
        base = Path(directory)
        directory_names.sort()
        file_names.sort()
        for name in directory_names:
            item = base / name
            if item.is_symlink():
                raise ValueError(f"formal output must not contain symlinks: {item}")
            if not stat.S_ISDIR(item.stat(follow_symlinks=False).st_mode):
                raise ValueError(
                    f"formal output contains a non-directory entry: {item}"
                )
        for name in file_names:
            item = base / name
            if item.is_symlink():
                raise ValueError(f"formal output must not contain symlinks: {item}")
            if not stat.S_ISREG(item.stat(follow_symlinks=False).st_mode):
                raise ValueError(f"formal output contains a non-regular file: {item}")
            files.append(item)
    if not files:
        raise ValueError(f"formal output directory is empty: {resolved}")

    digest = hashlib.sha256()
    for item in sorted(files, key=lambda value: value.relative_to(resolved).as_posix()):
        relative = item.relative_to(resolved).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(item).encode("ascii"))
        digest.update(b"\n")
    return f"sha256:{digest.hexdigest()}"


def _sha256_file(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"formal output must contain regular files only: {path}")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if identity_before != identity_after:
            raise RuntimeError(f"formal output changed while hashing: {path}")
    finally:
        os.close(descriptor)
    return f"sha256:{digest.hexdigest()}"


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _read_json_object(path: Path, *, description: str) -> dict[str, object]:
    candidate = Path(path)
    try:
        value = json.loads(_read_regular_bytes(candidate).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {description}: {candidate}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{description} must be a JSON object: {candidate}")
    return cast(dict[str, object], value)


def _find_formal_output_root(artifact: Path) -> tuple[Path, Path]:
    resolved = _resolve_formal_output(artifact)
    roots = (resolved, resolved.parent) if resolved.is_file() else (resolved,)
    for root in roots:
        attestation = formal_artifact_attestation_path(root)
        if os.path.lexists(attestation):
            if attestation.is_symlink() or not attestation.is_file():
                raise ValueError(
                    f"formal MLflow success attestation is not a regular file: {attestation}"
                )
            return root, attestation
    raise ValueError(
        f"formal MLflow success attestation is missing: artifact={resolved}"
    )


def _write_json_create_only(path: Path, payload: Mapping[str, object]) -> Path:
    candidate = Path(path).expanduser()
    parent = candidate.parent.resolve(strict=True)
    output = parent / candidate.name
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{output.name}.",
            suffix=".new.tmp",
            dir=output.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(
                payload,
                stream,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, output)
        except FileExistsError as error:
            raise FileExistsError(
                f"create-only artifact already exists: {output}"
            ) from error
        directory_descriptor = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output


def _end_failed(logger: ExperimentLogger) -> None:
    try:
        logger.end(status="FAILED")
    except Exception:
        pass


def _read_regular_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"expected a regular file: {path}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RuntimeError(f"file changed while reading: {path}")
    finally:
        os.close(descriptor)
    return b"".join(chunks)


__all__ = [
    "AttestationFactory",
    "AttestationParser",
    "FormalArtifactAttestation",
    "OutputKind",
    "formal_artifact_attestation_path",
    "publish_formal_artifact",
    "verify_formal_artifact",
]
