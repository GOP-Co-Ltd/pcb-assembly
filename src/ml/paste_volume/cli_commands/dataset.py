"""Dataset commands for the paste-volume ML workflow."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def parse_source(value: str) -> tuple[str, Path]:
    source_id, separator, raw_path = value.partition("=")
    if not separator or not source_id or not raw_path:
        raise argparse.ArgumentTypeError("source must be SOURCE_ID=PATH")
    return source_id, Path(raw_path).expanduser().resolve()


def _dataset_inputs(paths: Sequence[str]) -> list[Any]:
    from ml.paste_volume.data import DatasetInput

    return [
        DatasetInput(source_id=None, path=Path(path).expanduser().resolve())
        for path in paths
    ]


def dataset_merge(args: argparse.Namespace) -> Any:
    from ml.paste_volume.data import DatasetInput, merge_datasets

    source_ids = [source_id for source_id, _ in args.source]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("dataset source IDs must be unique")
    inputs = [
        DatasetInput(source_id=source_id, path=path) for source_id, path in args.source
    ]
    output = Path(args.output).expanduser().resolve()
    return merge_datasets(inputs, output, name=args.name or output.stem)


def dataset_validate(args: argparse.Namespace) -> Any:
    from ml.paste_volume.data import validate_datasets

    return validate_datasets(_dataset_inputs(args.datasets))


def dataset_summarize(args: argparse.Namespace) -> Any:
    from ml.paste_volume.data import (
        resolve_dataset_inputs,
        summarize_dataset,
    )

    roots = [Path(path).expanduser().resolve() for path in args.datasets]
    composite = resolve_dataset_inputs(roots=roots)
    return summarize_dataset(composite)
