"""Schemaに依存しないPyTorch artifact I/O."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

import torch

type ArtifactValidator = Callable[[Mapping[str, object]], None]
type ReadbackValidator = Callable[
    [Mapping[str, object], Mapping[str, object], Path], None
]


def atomic_torch_save(
    payload: Mapping[str, object],
    path: Path,
    *,
    validate: ArtifactValidator,
    validate_readback: ReadbackValidator | None = None,
) -> None:
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
        loaded = load_torch_mapping(
            temporary,
            description="checkpoint write verification",
        )
        validate(loaded)
        if validate_readback is not None:
            validate_readback(payload, loaded, path)
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_torch_mapping(path: Path, *, description: str) -> dict[str, object]:
    """``weights_only``かつCPU mappingとしてPyTorch artifactを読む."""

    payload = torch.load(path, map_location="cpu", weights_only=True)
    if not isinstance(payload, dict):
        raise ValueError(f"{description} must be a mapping")
    return cast(dict[str, object], payload)


__all__ = [
    "ArtifactValidator",
    "ReadbackValidator",
    "atomic_torch_save",
    "load_torch_mapping",
]
