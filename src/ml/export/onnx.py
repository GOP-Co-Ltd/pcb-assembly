"""Model-agnostic ONNX export, graph inspection, and JSON metadata
mechanics."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast


@dataclass(frozen=True)
class OnnxGraphInfo:
    node_types: tuple[str, ...]
    opset_version: int


def export_dynamo_model(
    model: Any,
    example_inputs: Sequence[Any],
    output_path: Path,
    *,
    input_names: Sequence[str],
    output_names: Sequence[str],
    dynamic_shapes: Any,
    opset_version: int,
) -> Path:
    """Export a model with PyTorch's dynamo exporter as one self-contained
    file."""

    if type(opset_version) is not int or opset_version < 1:
        raise ValueError("opset_version must be a positive integer")
    if not input_names or len(set(input_names)) != len(input_names):
        raise ValueError("ONNX input names must be non-empty and unique")
    if not output_names or len(set(output_names)) != len(output_names):
        raise ValueError("ONNX output names must be non-empty and unique")
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"ONNX output already exists: {destination}")

    import torch

    try:
        torch.onnx.export(
            model,
            tuple(example_inputs),
            destination,
            input_names=tuple(input_names),
            output_names=tuple(output_names),
            dynamic_shapes=dynamic_shapes,
            opset_version=opset_version,
            dynamo=True,
            external_data=False,
        )
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


def inspect_onnx_model(
    path: Path,
    *,
    allowed_operators: Iterable[str] | None = None,
    allowed_domains: Iterable[str] = ("", "ai.onnx"),
) -> OnnxGraphInfo:
    """Run the ONNX checker and reject external data and custom graph
    surfaces."""

    import onnx

    model = onnx.load(str(path), load_external_data=False)
    if any(
        tensor.data_location == onnx.TensorProto.EXTERNAL or tensor.external_data
        for tensor in _iter_tensors(model)
    ):
        raise ValueError("ONNX external data artifacts are not allowed")
    onnx.checker.check_model(model, full_check=True)

    domains = frozenset(allowed_domains)
    forbidden_domains = sorted(
        {node.domain for node in model.graph.node if node.domain not in domains}
    )
    if forbidden_domains:
        raise ValueError(
            f"custom ONNX operator domains are not allowed: {forbidden_domains}"
        )
    node_types = {node.op_type for node in model.graph.node}
    if allowed_operators is not None:
        unsupported = sorted(node_types - frozenset(allowed_operators))
        if unsupported:
            raise ValueError(f"unsupported ONNX operators: {unsupported}")
    return OnnxGraphInfo(
        node_types=tuple(sorted(node_types)),
        opset_version=standard_opset_version(model),
    )


def standard_opset_version(model_or_path: Any) -> int:
    """Return the highest standard-domain opset from a loaded model or path."""

    if isinstance(model_or_path, (str, Path)):
        import onnx

        model = onnx.load(str(model_or_path), load_external_data=False)
    else:
        model = model_or_path
    versions = [
        item.version for item in model.opset_import if item.domain in {"", "ai.onnx"}
    ]
    if not versions:
        raise ValueError("ONNX model has no standard opset")
    return max(versions)


def onnx_version() -> str:
    """Return the installed ONNX library version."""

    import onnx

    return onnx.__version__


def write_json_metadata(
    path: Path,
    *,
    key: str,
    value: Mapping[str, Any],
    clear_existing: bool = False,
) -> None:
    """Atomically replace one JSON metadata property inside a self-contained
    model."""

    if not key:
        raise ValueError("ONNX metadata key must be non-empty")
    import onnx

    model = onnx.load(str(path), load_external_data=False)
    retained = (
        ()
        if clear_existing
        else tuple(
            (entry.key, entry.value)
            for entry in model.metadata_props
            if entry.key != key
        )
    )
    del model.metadata_props[:]
    for retained_key, retained_value in retained:
        entry = model.metadata_props.add()
        entry.key = retained_key
        entry.value = retained_value
    entry = model.metadata_props.add()
    entry.key = key
    entry.value = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    destination = Path(path)
    original_mode = stat.S_IMODE(destination.stat().st_mode)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        onnx.save(model, str(temporary), save_as_external_data=False)
        temporary.chmod(original_mode)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def read_json_metadata(path: Path, *, key: str) -> Mapping[str, Any]:
    """Read one JSON object metadata property without applying a domain
    schema."""

    import onnx

    model = onnx.load(str(path), load_external_data=False)
    raw_values = [entry.value for entry in model.metadata_props if entry.key == key]
    if len(raw_values) != 1:
        raise ValueError(f"ONNX metadata property must occur exactly once: {key}")
    try:
        value = json.loads(raw_values[0])
    except json.JSONDecodeError as exc:
        raise ValueError(f"ONNX metadata property is not valid JSON: {key}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"ONNX metadata property must contain an object: {key}")
    return cast(Mapping[str, Any], value)


def _iter_tensors(message: Any) -> Iterable[Any]:
    descriptor = message.DESCRIPTOR
    if descriptor.full_name == "onnx.TensorProto":
        yield message
        return
    for field, value in message.ListFields():
        if field.message_type is None:
            continue
        if field.is_repeated:
            for item in value:
                yield from _iter_tensors(item)
        else:
            yield from _iter_tensors(value)
