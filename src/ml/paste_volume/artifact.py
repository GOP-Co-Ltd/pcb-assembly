"""Paste-volume artifact identity, schema, and JSON wire helpers."""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, TypeGuard, cast

import attrs

from ml.infer.package import canonical_json_sha256, sha256_file

type ModelFormat = Literal["onnx-fp32", "onnx-int8-qdq"]
type OnnxArtifactRole = Literal["export-fp32", "optimized-fp32", "int8-qdq"]

EVALUATION_SCHEMA_VERSION = 2
BENCHMARK_SCHEMA_VERSION = 1
MODEL_PACKAGE_SCHEMA_VERSION = 2


@attrs.frozen
class ArtifactLineage:
    """Training identity propagated unchanged through every release
    artifact."""

    source_run_id: str
    source_checkpoint_sha256: str
    dataset_fingerprint: str
    split_fingerprint: str
    training_protocol_fingerprint: str
    source_checkpoint_role: Literal["best"] = "best"
    parent_run_id: str | None = None
    parent_checkpoint_id: str | None = None

    def __attrs_post_init__(self) -> None:
        for name in (
            "source_run_id",
            "source_checkpoint_sha256",
            "dataset_fingerprint",
            "split_fingerprint",
        ):
            if not getattr(self, name):
                raise ValueError(f"{name}を空にできません")
        if (self.parent_run_id is None) != (self.parent_checkpoint_id is None):
            raise ValueError("fine-tune parent run/checkpointは両方指定してください")
        if self.source_checkpoint_role != "best":
            raise ValueError("export source checkpoint roleはbestだけを許可します")
        for name in (
            "source_checkpoint_sha256",
            "dataset_fingerprint",
            "split_fingerprint",
        ):
            if not is_prefixed_sha256(getattr(self, name)):
                raise ValueError(f"{name}が不正です")
        if not is_prefixed_sha256(self.training_protocol_fingerprint):
            raise ValueError("training_protocol_fingerprintが不正です")

    def to_dict(self) -> dict[str, str | None]:
        return attrs.asdict(self)


def validate_canonical_preprocess_schema(schema: Mapping[str, Any]) -> None:
    """Validate the immutable paste-volume v1 preprocessing contract."""

    expected = {
        "schema_version",
        "channel_order",
        "input_channels",
        "normalization",
        "image_constraints",
    }
    if set(schema) != expected:
        raise ValueError("preprocess schema key集合が不正です")
    if schema.get("schema_version") != 1:
        raise ValueError("preprocess schema_versionが不正です")
    if schema.get("channel_order") != "RGB" or schema.get("input_channels") != 6:
        raise ValueError("preprocess RGB/6-channel契約が不正です")
    normalization = required_mapping(schema, "normalization")
    if set(normalization) != {
        "kind",
        "axes",
        "affine",
        "per_channel",
        "epsilon",
    }:
        raise ValueError("preprocess normalization key集合が不正です")
    if (
        normalization.get("kind") != "sample-layer-norm"
        or normalization.get("axes") != [0, 1, 2]
        or normalization.get("affine") is not False
        or normalization.get("per_channel") is not False
        or not is_positive_finite(normalization.get("epsilon"))
    ):
        raise ValueError("SampleLayerNorm契約が不正です")
    constraints = required_mapping(schema, "image_constraints")
    if set(constraints) != {
        "min_size",
        "max_size",
        "max_pixels",
        "stride",
        "normalization_epsilon",
    }:
        raise ValueError("image constraints key集合が不正です")
    min_size = constraints.get("min_size")
    max_size = constraints.get("max_size")
    max_pixels = constraints.get("max_pixels")
    stride = constraints.get("stride")
    epsilon = constraints.get("normalization_epsilon")
    if (
        type(min_size) is not int
        or type(max_size) is not int
        or type(max_pixels) is not int
        or type(stride) is not int
        or min_size < 32
        or max_size < min_size
        or max_size > 1024
        or max_pixels < min_size * min_size
        or max_pixels > 262_144
        or stride < 1
        or stride > max_size
        or not is_positive_finite(epsilon)
        or float(epsilon) != float(normalization["epsilon"])
    ):
        raise ValueError("image constraintsがv1安全範囲を満たしません")


def image_constraints_from_schema(schema: Mapping[str, Any]) -> Any:
    validate_canonical_preprocess_schema(schema)
    from .data import ImageConstraints

    raw = required_mapping(schema, "image_constraints")
    return ImageConstraints(
        min_size=int(raw["min_size"]),
        max_size=int(raw["max_size"]),
        max_pixels=int(raw["max_pixels"]),
        stride=int(raw["stride"]),
        normalization_epsilon=float(raw["normalization_epsilon"]),
    )


def lineage_from_mapping(value: Mapping[str, Any]) -> ArtifactLineage:
    return ArtifactLineage(
        source_run_id=required_string(value, "source_run_id"),
        source_checkpoint_sha256=required_string(value, "source_checkpoint_sha256"),
        dataset_fingerprint=required_string(value, "dataset_fingerprint"),
        split_fingerprint=required_string(value, "split_fingerprint"),
        training_protocol_fingerprint=required_string(
            value, "training_protocol_fingerprint"
        ),
        source_checkpoint_role=required_best_role(value),
        parent_run_id=optional_string(value, "parent_run_id"),
        parent_checkpoint_id=optional_string(value, "parent_checkpoint_id"),
    )


def required_mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    item = value.get(key)
    if not isinstance(item, dict):
        raise ValueError(f"{key}はmappingが必要です")
    return cast(Mapping[str, Any], item)


def required_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{key}は空でない文字列が必要です")
    return item


def required_best_role(value: Mapping[str, Any]) -> Literal["best"]:
    if value.get("source_checkpoint_role") != "best":
        raise ValueError("source_checkpoint_roleはbestが必要です")
    return "best"


def optional_string(value: Mapping[str, Any], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or not item:
        raise ValueError(f"{key}が不正です")
    return item


def canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def fingerprint_json(value: Mapping[str, Any]) -> str:
    return canonical_json_sha256(value)


def artifact_sha256(path: Path) -> str:
    return sha256_file(path)


def is_sha256(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def is_prefixed_sha256(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str) and value.startswith("sha256:") and is_sha256(value[7:])
    )


def is_finite_number(value: object) -> TypeGuard[int | float]:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def is_positive_finite(value: object) -> TypeGuard[int | float]:
    return is_finite_number(value) and value > 0


def is_nonnegative_finite(value: object) -> TypeGuard[int | float]:
    return is_finite_number(value) and value >= 0


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"JSON artifactを読めません: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifactはobjectが必要です: {path}")
    return cast(Mapping[str, Any], value)


def atomic_json_new(path: Path, value: Mapping[str, Any]) -> None:
    """Publish one report create-only, preserving an existing input
    artifact."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(path):
        raise FileExistsError(f"artifact outputが既に存在します: {path}")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
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
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
