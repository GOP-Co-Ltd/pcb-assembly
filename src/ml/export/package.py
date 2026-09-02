"""Transactional immutable-package publication and active-pointer switching."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ml.infer.package import (
    ActivePointerRecord,
    ImmutablePackageError,
    load_active_pointer,
    sha256_file,
)


@dataclass(frozen=True)
class PublishedPackage:
    path: Path
    checksums: Mapping[str, str]


def publish_immutable_package(
    output_directory: Path,
    *,
    payload_files: Iterable[str],
    write_payloads: Callable[[Path], None],
    checksum_file: str = "SHA256SUMS",
) -> PublishedPackage:
    """Build a complete package privately, add checksums, then rename it
    once."""

    if not checksum_file or Path(checksum_file).name != checksum_file:
        raise ValueError("checksum_file must be a plain file name")
    payload_names = tuple(payload_files)
    filenames = frozenset(payload_names)
    if (
        not filenames
        or len(filenames) != len(payload_names)
        or checksum_file in filenames
        or any(not name or Path(name).name != name for name in filenames)
    ):
        raise ValueError("payload_files must contain plain, unique file names")
    destination = Path(output_directory).expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"model package already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    try:
        write_payloads(temporary)
        actual = {entry.name for entry in temporary.iterdir()}
        if actual != filenames:
            raise ValueError("package writer did not create the exact payload file set")
        for filename in filenames:
            path = temporary / filename
            if not path.is_file() or path.is_symlink():
                raise ValueError(f"package payload is not a regular file: {filename}")
        checksums = {
            filename: sha256_file(temporary / filename)
            for filename in sorted(filenames)
        }
        (temporary / checksum_file).write_text(
            "".join(
                f"{digest}  {filename}\n" for filename, digest in checksums.items()
            ),
            encoding="utf-8",
        )
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return PublishedPackage(path=destination, checksums=checksums)


def switch_active_pointer(
    pointer_file: Path,
    active_package: Path,
    *,
    active_package_id: str,
    active_package_sha256: str,
    schema_version: int,
) -> ActivePointerRecord:
    """Atomically point at a verified package while retaining one
    predecessor."""

    pointer_path = Path(pointer_file).expanduser().resolve()
    package_path = Path(active_package).expanduser().resolve()
    if pointer_path.is_relative_to(package_path):
        raise ValueError(
            "active model pointer must be outside the immutable model package"
        )
    if not active_package_id:
        raise ValueError("active_package_id must be non-empty")
    if len(active_package_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in active_package_sha256
    ):
        raise ValueError("active_package_sha256 must be an unprefixed SHA-256")

    previous: Path | None = None
    if pointer_path.exists():
        try:
            current = load_active_pointer(pointer_path, schema_version=schema_version)
        except ImmutablePackageError as exc:
            raise ValueError(str(exc)) from exc
        previous = current.active_package_path
        if previous == package_path:
            previous = current.previous_package_path
    value = {
        "schema_version": schema_version,
        "active_model_path": str(package_path),
        "previous_model_path": None if previous is None else str(previous),
        "active_model_id": active_package_id,
        "active_model_sha256": active_package_sha256,
        "updated_at_unix_ns": time.time_ns(),
    }
    atomic_json_write(pointer_path, value)
    return ActivePointerRecord(
        pointer_path=pointer_path,
        active_package_path=package_path,
        previous_package_path=previous,
        active_package_id=active_package_id,
        active_package_sha256=active_package_sha256,
    )


def atomic_json_write(path: Path, value: Mapping[str, Any]) -> None:
    """Durably replace one JSON object without exposing a partial pointer."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(
                value,
                stream,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
