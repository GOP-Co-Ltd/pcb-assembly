"""Offline verification mechanics for immutable model packages and pointers."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ImmutablePackageError(ValueError):
    """An immutable package or active pointer is malformed."""


@dataclass(frozen=True)
class VerifiedPackageFiles:
    """Filesystem facts established before a domain manifest is interpreted."""

    path: Path
    checksums: Mapping[str, str]


@dataclass(frozen=True)
class ActivePointerRecord:
    """Model-agnostic wire representation of an active package pointer."""

    pointer_path: Path
    active_package_path: Path
    previous_package_path: Path | None
    active_package_id: str
    active_package_sha256: str


def verify_immutable_package(
    package: Path,
    *,
    payload_files: Iterable[str],
    checksum_file: str = "SHA256SUMS",
) -> VerifiedPackageFiles:
    """Verify exact entries, regular files, and SHA-256 payload checksums.

    Domain schemas are deliberately outside this function.  Callers
    first establish the immutable filesystem envelope here, then
    validate their manifest payload.
    """

    if not checksum_file or Path(checksum_file).name != checksum_file:
        raise ValueError("checksum_file must be a plain file name")
    package_path = Path(package).expanduser().resolve()
    payload_names = tuple(payload_files)
    expected_payloads = frozenset(payload_names)
    if (
        not expected_payloads
        or len(expected_payloads) != len(payload_names)
        or checksum_file in expected_payloads
        or any(not name or Path(name).name != name for name in expected_payloads)
    ):
        raise ValueError("payload_files must contain plain, unique file names")
    if not package_path.is_dir():
        raise ImmutablePackageError(
            f"model package directory does not exist: {package_path}"
        )

    expected_entries = {*expected_payloads, checksum_file}
    try:
        actual_entries = {entry.name for entry in package_path.iterdir()}
    except OSError as exc:
        raise ImmutablePackageError(f"model package cannot be listed: {exc}") from exc
    if actual_entries != expected_entries:
        raise ImmutablePackageError(
            f"model packageは規定の{len(expected_entries)} fileだけを含めてください"
        )
    for filename in expected_entries:
        path = package_path / filename
        if not path.is_file() or path.is_symlink():
            raise ImmutablePackageError(
                f"model package entry is missing, not regular, or a symlink: {filename}"
            )

    checksums = read_sha256sums(
        package_path / checksum_file,
        allowed_files=expected_payloads,
    )
    if set(checksums) != expected_payloads:
        raise ImmutablePackageError(
            "SHA256SUMS must contain every defined payload file exactly once"
        )
    for filename, expected in checksums.items():
        actual = sha256_file(package_path / filename)
        if actual != expected:
            raise ImmutablePackageError(f"model package checksum mismatch: {filename}")
    return VerifiedPackageFiles(path=package_path, checksums=checksums)


def read_sha256sums(path: Path, *, allowed_files: Iterable[str]) -> dict[str, str]:
    """Read the strict two-space ``sha256sum`` package format."""

    allowed = frozenset(allowed_files)
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ImmutablePackageError(f"SHA256SUMS cannot be read: {exc}") from exc
    for line in lines:
        parts = line.split("  ")
        if len(parts) != 2 or _SHA256_PATTERN.fullmatch(parts[0]) is None:
            raise ImmutablePackageError("SHA256SUMS format is invalid")
        filename = parts[1]
        if filename not in allowed or filename in result:
            raise ImmutablePackageError(
                "SHA256SUMS contains an invalid or duplicate file name"
            )
        result[filename] = parts[0]
    return result


def load_active_pointer(
    pointer_file: Path, *, schema_version: int
) -> ActivePointerRecord:
    """Parse pointer mechanics without loading or interpreting a model
    package."""

    pointer_path = Path(pointer_file).expanduser().resolve(strict=True)
    raw = read_json_object(pointer_path, description="active model pointer")
    if raw.get("schema_version") != schema_version:
        raise ImmutablePackageError("unknown active model pointer schema")

    active_raw = _required_string(raw, "active_model_path")
    active_path = Path(active_raw)
    if not active_path.is_absolute():
        active_path = pointer_path.parent / active_path
    active_path = active_path.resolve()
    if pointer_path.is_relative_to(active_path):
        raise ImmutablePackageError(
            "active model pointer must be outside the immutable model package"
        )

    previous_raw = raw.get("previous_model_path")
    if previous_raw is not None and not isinstance(previous_raw, str):
        raise ImmutablePackageError("previous_model_path is invalid")
    previous_path = None
    if previous_raw is not None:
        previous_path = Path(previous_raw)
        if not previous_path.is_absolute():
            previous_path = pointer_path.parent / previous_path
        previous_path = previous_path.resolve()

    checksum = _required_string(raw, "active_model_sha256")
    if _SHA256_PATTERN.fullmatch(checksum) is None:
        raise ImmutablePackageError("active model checksum is invalid")
    return ActivePointerRecord(
        pointer_path=pointer_path,
        active_package_path=active_path,
        previous_package_path=previous_path,
        active_package_id=_required_string(raw, "active_model_id"),
        active_package_sha256=checksum,
    )


def read_json_object(path: Path, *, description: str) -> Mapping[str, Any]:
    """Read one JSON object while preserving a useful artifact error
    boundary."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ImmutablePackageError(f"{description} cannot be read: {exc}") from exc
    if not isinstance(value, dict):
        raise ImmutablePackageError(f"{description} must be a JSON object")
    return cast(Mapping[str, Any], value)


def sha256_file(path: Path) -> str:
    """Return an unprefixed SHA-256 digest for a regular artifact file."""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
    except OSError as exc:
        raise ImmutablePackageError(f"file cannot be hashed: {path}: {exc}") from exc
    return digest.hexdigest()


def canonical_json_sha256(value: Mapping[str, Any]) -> str:
    """Hash a JSON mapping using the artifact canonicalization contract."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _required_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ImmutablePackageError(f"{key} must be a non-empty string")
    return item
