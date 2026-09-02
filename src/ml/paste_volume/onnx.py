"""Paste-volume ONNX export, parity evidence, and optimization policy."""

from __future__ import annotations

import math
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, cast, override

import attrs
import numpy as np

from ml.export.onnx import (
    export_dynamo_model,
    inspect_onnx_model,
    onnx_version,
    read_json_metadata,
    standard_opset_version,
    write_json_metadata,
)
from ml.export.optimize import optimize_fp32, quantize_static_qdq
from ml.export.parity import compare_eager_outputs
from ml.infer.onnx import OnnxSession, onnxruntime_version

from .artifact import (
    ArtifactLineage,
    ModelFormat,
    OnnxArtifactRole,
    artifact_sha256 as _sha256_file,
    atomic_json_new as _atomic_json_new,
    canonical_json as _canonical_json,
    fingerprint_json as _canonical_json_sha256,
    image_constraints_from_schema as _image_constraints_from_schema,
    is_finite_number as _is_finite_number,
    is_nonnegative_finite as _is_nonnegative_finite,
    is_prefixed_sha256 as _is_prefixed_sha256,
    is_sha256 as _is_sha256,
    lineage_from_mapping as _lineage_from_mapping,
    read_json as _read_json,
    required_best_role as _required_best_role,
    required_mapping as _required_mapping,
    required_string as _required_string,
    validate_canonical_preprocess_schema as _validate_canonical_preprocess_schema,
)
from .formal_artifact import FormalArtifactAttestation
from .training_artifacts import FormalTrainingWeights

EXPORT_PARITY_REPORT_SCHEMA_VERSION = 1
DEFAULT_OPSET_VERSION = 18
EXPORT_PARITY_REPORT_KIND = "pcbasm-paste-volume-export-parity"
FP32_RELATIVE_TOLERANCE = 0.001
FP32_ABSOLUTE_TOLERANCE_UL = 0.000001
_ALLOWED_ONNX_OPERATORS = frozenset(
    {
        "Add",
        "Cast",
        "Clip",
        "Concat",
        "Constant",
        "Conv",
        "DequantizeLinear",
        "Div",
        "Exp",
        "Flatten",
        "Gather",
        "Gemm",
        "GlobalAveragePool",
        "Greater",
        "Identity",
        "InstanceNormalization",
        "Log",
        "MatMul",
        "Max",
        "Mul",
        "Pow",
        "QuantizeLinear",
        "ReduceMax",
        "ReduceMean",
        "ReduceMin",
        "Relu",
        "Reshape",
        "Shape",
        "Sigmoid",
        "Slice",
        "Softplus",
        "Sqrt",
        "Squeeze",
        "Sub",
        "Transpose",
        "Unsqueeze",
        "Where",
    }
)
type ExportParityShapeCase = Literal["minimum", "maximum-area", "portrait", "landscape"]
_EXPORT_PARITY_SHAPES: tuple[tuple[ExportParityShapeCase, tuple[int, int]], ...] = (
    ("minimum", (32, 32)),
    ("maximum-area", (512, 512)),
    ("portrait", (1024, 256)),
    ("landscape", (256, 1024)),
)


@attrs.frozen
class OnnxExportResult:
    weights_path: Path
    model_path: Path
    model_artifact_sha256: str
    opset_version: int
    node_types: tuple[str, ...]
    dynamic_shapes: tuple[tuple[int, int], ...]
    lineage: ArtifactLineage
    model_config: Mapping[str, Any]
    preprocess_schema: Mapping[str, Any]
    uncertainty_log_variance_offset: float

    def to_dict(self) -> dict[str, object]:
        return {
            "weights_path": str(self.weights_path),
            "model_path": str(self.model_path),
            "model_artifact_sha256": self.model_artifact_sha256,
            "opset_version": self.opset_version,
            "node_types": list(self.node_types),
            "dynamic_shapes": [list(shape) for shape in self.dynamic_shapes],
            "lineage": self.lineage.to_dict(),
            "model_config": dict(self.model_config),
            "preprocess_schema": dict(self.preprocess_schema),
            "uncertainty_log_variance_offset": self.uncertainty_log_variance_offset,
        }


@attrs.frozen
class OnnxParityResult:
    model_artifact_sha256: str
    shapes: tuple[tuple[int, int], ...]
    maximum_mean_absolute_error_ul: float
    maximum_log_variance_absolute_error: float
    passed: bool

    def to_dict(self) -> dict[str, object]:
        return attrs.asdict(self)


@attrs.frozen
class ExportParitySampleResult:
    """One persisted validation sample's eager-vs-FP32 ONNX comparison."""

    sample_id: str
    mean_absolute_error_ul: float
    mean_tolerance_ul: float
    log_variance_absolute_error: float
    log_variance_tolerance: float
    passed: bool

    def __attrs_post_init__(self) -> None:
        if not self.sample_id:
            raise ValueError("export parity sample_idを空にできません")
        if type(self.passed) is not bool:
            raise ValueError("export parity sample passedはboolが必要です")
        for name in (
            "mean_absolute_error_ul",
            "mean_tolerance_ul",
            "log_variance_absolute_error",
            "log_variance_tolerance",
        ):
            value = getattr(self, name)
            if not _is_nonnegative_finite(value):
                raise ValueError(f"export parity {name}が不正です")
        if self.mean_tolerance_ul <= 0 or self.log_variance_tolerance <= 0:
            raise ValueError("export parity toleranceは正の有限値が必要です")
        expected = (
            self.mean_absolute_error_ul <= self.mean_tolerance_ul
            and self.log_variance_absolute_error <= self.log_variance_tolerance
        )
        if self.passed is not expected:
            raise ValueError("export parity sample gate statusが数値と不一致です")

    def to_dict(self) -> dict[str, object]:
        return attrs.asdict(self)


@attrs.frozen
class ExportParityShapeResult:
    """One required dynamic-shape eager-vs-FP32 ONNX comparison."""

    case_name: ExportParityShapeCase
    image_height: int
    image_width: int
    mean_absolute_error_ul: float
    mean_tolerance_ul: float
    log_variance_absolute_error: float
    log_variance_tolerance: float
    passed: bool

    def __attrs_post_init__(self) -> None:
        expected_shapes = dict(_EXPORT_PARITY_SHAPES)
        if self.case_name not in expected_shapes:
            raise ValueError("export parity dynamic shape caseが不正です")
        if (self.image_height, self.image_width) != expected_shapes[self.case_name]:
            raise ValueError("export parity dynamic shape寸法がcaseと不一致です")
        if type(self.passed) is not bool:
            raise ValueError("export parity dynamic shape passedはboolが必要です")
        for name in (
            "mean_absolute_error_ul",
            "mean_tolerance_ul",
            "log_variance_absolute_error",
            "log_variance_tolerance",
        ):
            if not _is_nonnegative_finite(getattr(self, name)):
                raise ValueError(f"export parity dynamic shape {name}が不正です")
        if self.mean_tolerance_ul <= 0 or self.log_variance_tolerance <= 0:
            raise ValueError(
                "export parity dynamic shape toleranceは正の有限値が必要です"
            )
        expected = (
            self.mean_absolute_error_ul <= self.mean_tolerance_ul
            and self.log_variance_absolute_error <= self.log_variance_tolerance
        )
        if self.passed is not expected:
            raise ValueError(
                "export parity dynamic shape gate statusが数値と不一致です"
            )

    def to_dict(self) -> dict[str, object]:
        return attrs.asdict(self)


@attrs.frozen
class ExportParityReport:
    """Validation samples and required dynamic shapes bound to export
    parity."""

    kind: str
    schema_version: int
    weights_sha256: str
    fp32_model_artifact_sha256: str
    training_protocol_fingerprint: str
    dataset_fingerprint: str
    split_fingerprint: str
    split_name: Literal["validation"]
    sample_ids: tuple[str, ...]
    training_sample_ids: tuple[str, ...]
    sample_results: tuple[ExportParitySampleResult, ...]
    shape_results: tuple[ExportParityShapeResult, ...]
    maximum_mean_absolute_error_ul: float
    maximum_log_variance_absolute_error: float
    success: bool
    content_sha256: str

    def __attrs_post_init__(self) -> None:
        if self.kind != EXPORT_PARITY_REPORT_KIND:
            raise ValueError("export parity report kindが不正です")
        if self.schema_version != EXPORT_PARITY_REPORT_SCHEMA_VERSION:
            raise ValueError("export parity report schemaが不正です")
        if not _is_prefixed_sha256(self.weights_sha256):
            raise ValueError("export parity weights_sha256が不正です")
        if not _is_sha256(self.fp32_model_artifact_sha256):
            raise ValueError("export parity FP32 model hashが不正です")
        for name in (
            "training_protocol_fingerprint",
            "dataset_fingerprint",
            "split_fingerprint",
        ):
            if not _is_prefixed_sha256(getattr(self, name)):
                raise ValueError(f"export parity {name}が不正です")
        if self.split_name != "validation":
            raise ValueError("export parityはvalidation assignmentだけを許可します")
        if not self.sample_ids or tuple(sorted(self.sample_ids)) != self.sample_ids:
            raise ValueError("export parity sample IDsは空でないsort済み集合が必要です")
        if len(set(self.sample_ids)) != len(self.sample_ids):
            raise ValueError("export parity sample IDsが重複しています")
        if (
            not self.training_sample_ids
            or tuple(sorted(self.training_sample_ids)) != self.training_sample_ids
            or len(set(self.training_sample_ids)) != len(self.training_sample_ids)
        ):
            raise ValueError(
                "export parity training sample IDsは空でないsort済み集合が必要です"
            )
        if tuple(item.sample_id for item in self.sample_results) != self.sample_ids:
            raise ValueError("export parity sample resultがassignmentと一致しません")
        expected_shapes = tuple(_EXPORT_PARITY_SHAPES)
        actual_shapes = tuple(
            (item.case_name, (item.image_height, item.image_width))
            for item in self.shape_results
        )
        if actual_shapes != expected_shapes:
            raise ValueError(
                "export parityにはminimum/maximum-area/portrait/landscapeが必要です"
            )
        all_results = (*self.sample_results, *self.shape_results)
        expected_mean = max(item.mean_absolute_error_ul for item in all_results)
        expected_log_variance = max(
            item.log_variance_absolute_error for item in all_results
        )
        if (
            self.maximum_mean_absolute_error_ul != expected_mean
            or self.maximum_log_variance_absolute_error != expected_log_variance
        ):
            raise ValueError("export parity aggregate errorがsample結果と不一致です")
        if type(self.success) is not bool or self.success is not all(
            item.passed for item in all_results
        ):
            raise ValueError("export parity successがsample/shape結果と不一致です")

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "weights_sha256": self.weights_sha256,
            "fp32_model_artifact_sha256": self.fp32_model_artifact_sha256,
            "training_protocol_fingerprint": self.training_protocol_fingerprint,
            "dataset_fingerprint": self.dataset_fingerprint,
            "split_fingerprint": self.split_fingerprint,
            "split_name": self.split_name,
            "sample_ids": list(self.sample_ids),
            "training_sample_ids": list(self.training_sample_ids),
            "sample_results": [item.to_dict() for item in self.sample_results],
            "shape_results": [item.to_dict() for item in self.shape_results],
            "maximum_mean_absolute_error_ul": self.maximum_mean_absolute_error_ul,
            "maximum_log_variance_absolute_error": (
                self.maximum_log_variance_absolute_error
            ),
            "success": self.success,
            "content_sha256": self.content_sha256,
        }


@attrs.frozen
class OptimizationResult:
    source_model_path: Path
    source_model_artifact_sha256: str
    optimized_fp32_path: Path
    optimized_fp32_artifact_sha256: str
    int8_path: Path
    int8_artifact_sha256: str
    calibration_sample_ids: tuple[str, ...]
    calibration_session_ids: tuple[str, ...]
    calibration_sample_index_fingerprint: str
    split_manifest_path: Path
    lineage: ArtifactLineage

    def to_dict(self) -> dict[str, object]:
        return {
            "source_model_path": str(self.source_model_path),
            "source_model_artifact_sha256": self.source_model_artifact_sha256,
            "optimized_fp32_path": str(self.optimized_fp32_path),
            "optimized_fp32_artifact_sha256": self.optimized_fp32_artifact_sha256,
            "int8_path": str(self.int8_path),
            "int8_artifact_sha256": self.int8_artifact_sha256,
            "calibration_sample_ids": list(self.calibration_sample_ids),
            "calibration_session_ids": list(self.calibration_session_ids),
            "calibration_sample_index_fingerprint": (
                self.calibration_sample_index_fingerprint
            ),
            "split_manifest_path": str(self.split_manifest_path),
            "lineage": self.lineage.to_dict(),
        }


def resolve_evaluation_dataset(
    dataset_paths: Sequence[Path],
    split_manifest: Path,
    *,
    lineage: ArtifactLineage,
    require_training_lineage_match: bool,
) -> tuple[Any, tuple[Any, ...], Any]:
    from .data import (
        build_sample_index,
        load_split_manifest,
        resolve_dataset_inputs,
        validate_split_manifest,
    )

    paths = tuple(Path(path).expanduser().resolve() for path in dataset_paths)
    if not paths:
        raise ValueError("dataset_pathsを1件以上指定してください")
    if len(paths) == 1 and paths[0].is_file():
        composite = resolve_dataset_inputs(manifest=paths[0])
    else:
        composite = resolve_dataset_inputs(roots=paths)
    samples = build_sample_index(composite)
    split = load_split_manifest(
        Path(split_manifest).expanduser().resolve(strict=True),
        expected_composite_fingerprint=composite.composite_fingerprint,
    )
    validate_split_manifest(
        split,
        samples,
        expected_composite_fingerprint=composite.composite_fingerprint,
    )
    if (
        require_training_lineage_match
        and composite.composite_fingerprint != lineage.dataset_fingerprint
    ):
        raise ValueError("evaluation dataset fingerprintがtraining lineageと不一致です")
    if (
        require_training_lineage_match
        and split.split_fingerprint != lineage.split_fingerprint
    ):
        raise ValueError("evaluation split fingerprintがtraining lineageと不一致です")
    return composite, samples, split


def run_export_parity_from_dataset(
    weights_path: Path,
    fp32_model_path: Path,
    dataset_paths: Sequence[Path],
    split_manifest: Path,
    *,
    split_name: Literal["validation"] = "validation",
) -> ExportParityReport:
    """Evaluate eager-vs-exported FP32 ONNX on every persisted validation
    sample."""

    if split_name != "validation":
        raise ValueError("export parityはvalidation assignmentだけを許可します")
    import torch
    from torch.nn import functional as torch_functional

    from .data import load_preprocessed_sample

    weights_file = Path(weights_path).expanduser().resolve(strict=True)
    model_path = Path(fp32_model_path).expanduser().resolve(strict=True)
    weights = load_export_weights(weights_file)
    nodes = check_onnx_model(model_path)
    if "QuantizeLinear" in nodes or "DequantizeLinear" in nodes:
        raise ValueError("export parity referenceはFP32 ONNXである必要があります")
    metadata = read_onnx_export_metadata(model_path)
    if metadata.get("artifact_role") != "export-fp32":
        raise ValueError("export parityには元のFP32 export artifactが必要です")
    expected_weights_sha256 = "sha256:" + _sha256_file(weights_file)
    if metadata.get("source_weights_sha256") != expected_weights_sha256:
        raise ValueError("FP32 exportとstrict weights artifactが一致しません")
    if _lineage_from_mapping(_required_mapping(metadata, "lineage")) != weights.lineage:
        raise ValueError("FP32 exportとstrict weights training lineageが一致しません")
    if _canonical_json(_required_mapping(metadata, "preprocess_schema")) != (
        _canonical_json(weights.preprocess_schema)
    ):
        raise ValueError("FP32 exportとstrict weights preprocess schemaが一致しません")

    composite, samples, split = resolve_evaluation_dataset(
        dataset_paths,
        split_manifest,
        lineage=weights.lineage,
        require_training_lineage_match=True,
    )
    sample_ids = tuple(split.validation_sample_ids)
    training_sample_ids = tuple(split.train_sample_ids)
    if not sample_ids:
        raise ValueError("export parity validation splitにsampleがありません")
    by_id = {sample.sample_id: sample for sample in samples}
    constraints = _image_constraints_from_schema(weights.preprocess_schema)
    session = OnnxSession.load_cpu(model_path)
    sample_results: list[ExportParitySampleResult] = []
    first_processed: Any | None = None

    def compare(
        image_6ch: Any,
        valid_pixel_mask: Any,
        pixel_per_mm: float,
        *,
        context: str,
    ) -> tuple[float, float, float, float, bool]:
        eager_mean, eager_log_variance = weights.model(
            image_6ch[None, ...],
            valid_pixel_mask[None, ...],
            torch.tensor([[pixel_per_mm]], dtype=torch.float32),
        )
        eager_mean_value = float(eager_mean.item())
        eager_log_variance_value = float(
            eager_log_variance.item() + weights.uncertainty_log_variance_offset
        )
        actual_mean, actual_log_variance = run_processed_onnx(
            session,
            image_6ch,
            valid_pixel_mask,
            pixel_per_mm,
        )
        values = (
            eager_mean_value,
            eager_log_variance_value,
            actual_mean,
            actual_log_variance,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError(f"export parity outputが非有限です: {context}")
        mean_parity, log_variance_parity = compare_eager_outputs(
            (eager_mean_value, eager_log_variance_value),
            (actual_mean, actual_log_variance),
            absolute_tolerances=(
                FP32_ABSOLUTE_TOLERANCE_UL,
                FP32_ABSOLUTE_TOLERANCE_UL,
            ),
            relative_tolerances=(
                FP32_RELATIVE_TOLERANCE,
                FP32_RELATIVE_TOLERANCE,
            ),
        )
        return (
            mean_parity.absolute_error,
            mean_parity.tolerance,
            log_variance_parity.absolute_error,
            log_variance_parity.tolerance,
            mean_parity.passed and log_variance_parity.passed,
        )

    weights.model.eval()
    with torch.no_grad():
        for sample_id in sample_ids:
            processed = load_preprocessed_sample(
                by_id[sample_id], constraints=constraints, training=False
            )
            if first_processed is None:
                first_processed = processed
            (
                mean_error,
                mean_tolerance,
                log_variance_error,
                log_variance_tolerance,
                passed,
            ) = compare(
                processed.image_6ch,
                processed.valid_pixel_mask,
                processed.pixel_per_mm,
                context=f"sample_id={sample_id}",
            )
            sample_results.append(
                ExportParitySampleResult(
                    sample_id=sample_id,
                    mean_absolute_error_ul=mean_error,
                    mean_tolerance_ul=mean_tolerance,
                    log_variance_absolute_error=log_variance_error,
                    log_variance_tolerance=log_variance_tolerance,
                    passed=passed,
                )
            )
        if first_processed is None:  # pragma: no cover - guarded by sample_ids
            raise AssertionError(
                "export parity validation sampleがmaterializeされませんでした"
            )
        base_image = first_processed.image_6ch[None, ...]
        base_mask = first_processed.valid_pixel_mask[None, ...].to(dtype=torch.float32)
        shape_results: list[ExportParityShapeResult] = []
        for case_name, (height, width) in _EXPORT_PARITY_SHAPES:
            image = torch_functional.interpolate(
                base_image,
                size=(height, width),
                mode="bilinear",
                align_corners=False,
            )[0]
            mask = torch_functional.interpolate(
                base_mask,
                size=(height, width),
                mode="nearest",
            )[0].to(dtype=torch.bool)
            (
                mean_error,
                mean_tolerance,
                log_variance_error,
                log_variance_tolerance,
                passed,
            ) = compare(
                image,
                mask,
                first_processed.pixel_per_mm,
                context=f"shape_case={case_name}",
            )
            shape_results.append(
                ExportParityShapeResult(
                    case_name=case_name,
                    image_height=height,
                    image_width=width,
                    mean_absolute_error_ul=mean_error,
                    mean_tolerance_ul=mean_tolerance,
                    log_variance_absolute_error=log_variance_error,
                    log_variance_tolerance=log_variance_tolerance,
                    passed=passed,
                )
            )
    all_results = (*sample_results, *shape_results)
    report = ExportParityReport(
        kind=EXPORT_PARITY_REPORT_KIND,
        schema_version=EXPORT_PARITY_REPORT_SCHEMA_VERSION,
        weights_sha256=expected_weights_sha256,
        fp32_model_artifact_sha256=_sha256_file(model_path),
        training_protocol_fingerprint=(weights.lineage.training_protocol_fingerprint),
        dataset_fingerprint=composite.composite_fingerprint,
        split_fingerprint=split.split_fingerprint,
        split_name="validation",
        sample_ids=sample_ids,
        training_sample_ids=training_sample_ids,
        sample_results=tuple(sample_results),
        shape_results=tuple(shape_results),
        maximum_mean_absolute_error_ul=max(
            item.mean_absolute_error_ul for item in all_results
        ),
        maximum_log_variance_absolute_error=max(
            item.log_variance_absolute_error for item in all_results
        ),
        success=all(item.passed for item in all_results),
        content_sha256="",
    )
    content = report.to_dict()
    del content["content_sha256"]
    finalized = attrs.evolve(
        report,
        content_sha256="sha256:" + _canonical_json_sha256(content),
    )
    validate_export_parity_report(finalized)
    return finalized


def run_export_parity_from_formal_weights(
    formal_weights: FormalTrainingWeights,
    fp32_model_path: Path,
    dataset_paths: Sequence[Path],
    split_manifest: Path,
    *,
    split_name: Literal["validation"] = "validation",
) -> ExportParityReport:
    """Generate release parity only from a live-verified training artifact."""

    validate_formal_training_weights(formal_weights, formal_weights.weights_path)
    metadata = read_onnx_export_metadata(
        Path(fp32_model_path).expanduser().resolve(strict=True)
    )
    if metadata.get("training_weights_attestation") != (
        formal_weights.attestation.to_dict()
    ):
        raise ValueError("FP32 exportのformal training attestationが入力と不一致です")
    return run_export_parity_from_dataset(
        formal_weights.weights_path,
        fp32_model_path,
        dataset_paths,
        split_manifest,
        split_name=split_name,
    )


def run_processed_onnx(
    session: Any,
    image: Any,
    mask: Any,
    pixel_per_mm: float,
) -> tuple[float, float]:
    result = session.run(
        ["mean_volume_ul", "log_variance_volume_ul2"],
        {
            "image_6ch": image.detach().cpu().float().numpy()[None, ...],
            "valid_pixel_mask": mask.detach().cpu().numpy()[None, ...].astype(np.bool_),
            "pixel_per_mm": np.asarray([[pixel_per_mm]], dtype=np.float32),
        },
    )
    if len(result) != 2:
        raise RuntimeError("ONNX model output数が不正です")
    mean = float(np.asarray(result[0]).item())
    log_variance = float(np.asarray(result[1]).item())
    if not math.isfinite(mean) or mean <= 0 or not math.isfinite(log_variance):
        raise RuntimeError(
            "ONNX model outputが正の有限mean/有限log-varianceではありません"
        )
    return mean, log_variance


def read_onnx_artifact_lineage(model_path: Path) -> ArtifactLineage:
    """ONNX artifactへbind済みのtraining lineageを読む."""

    path = Path(model_path).expanduser().resolve(strict=True)
    return _lineage_from_mapping(
        _required_mapping(read_onnx_export_metadata(path), "lineage")
    )


def read_onnx_preprocess_schema(model_path: Path) -> Mapping[str, Any]:
    """ONNX artifactへbind済みのpreprocess schemaを読む."""

    path = Path(model_path).expanduser().resolve(strict=True)
    schema = _required_mapping(read_onnx_export_metadata(path), "preprocess_schema")
    _validate_canonical_preprocess_schema(schema)
    return schema


def validate_export_parity_report(report: ExportParityReport) -> None:
    """Validate a complete export parity report and its canonical content
    hash."""

    if not _is_prefixed_sha256(report.content_sha256):
        raise ValueError("export parity content_sha256が不正です")
    payload = report.to_dict()
    del payload["content_sha256"]
    expected = "sha256:" + _canonical_json_sha256(payload)
    if report.content_sha256 != expected:
        raise ValueError("export parity content_sha256が内容と一致しません")


def export_parity_report_from_dict(raw: Mapping[str, Any]) -> ExportParityReport:
    """Parse the strict v1 export parity schema."""

    expected = {
        "kind",
        "schema_version",
        "weights_sha256",
        "fp32_model_artifact_sha256",
        "training_protocol_fingerprint",
        "dataset_fingerprint",
        "split_fingerprint",
        "split_name",
        "sample_ids",
        "training_sample_ids",
        "sample_results",
        "shape_results",
        "maximum_mean_absolute_error_ul",
        "maximum_log_variance_absolute_error",
        "success",
        "content_sha256",
    }
    if set(raw) != expected:
        raise ValueError("export parity reportのkey集合が不正です")
    sample_ids = raw.get("sample_ids")
    training_sample_ids = raw.get("training_sample_ids")
    samples_raw = raw.get("sample_results")
    shapes_raw = raw.get("shape_results")
    if (
        not isinstance(sample_ids, list)
        or not all(isinstance(item, str) for item in sample_ids)
        or not isinstance(training_sample_ids, list)
        or not all(isinstance(item, str) for item in training_sample_ids)
        or not isinstance(samples_raw, list)
        or not isinstance(shapes_raw, list)
    ):
        raise ValueError("export parity sample assignmentが不正です")
    sample_expected = {
        "sample_id",
        "mean_absolute_error_ul",
        "mean_tolerance_ul",
        "log_variance_absolute_error",
        "log_variance_tolerance",
        "passed",
    }
    sample_results: list[ExportParitySampleResult] = []
    for index, value in enumerate(samples_raw):
        if not isinstance(value, dict) or set(value) != sample_expected:
            raise ValueError(f"export parity sample_results[{index}]が不正です")
        try:
            sample_results.append(ExportParitySampleResult(**value))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"export parity sample_results[{index}]が不正です: {exc}"
            ) from exc
    shape_expected = {
        "case_name",
        "image_height",
        "image_width",
        "mean_absolute_error_ul",
        "mean_tolerance_ul",
        "log_variance_absolute_error",
        "log_variance_tolerance",
        "passed",
    }
    shape_results: list[ExportParityShapeResult] = []
    for index, value in enumerate(shapes_raw):
        if not isinstance(value, dict) or set(value) != shape_expected:
            raise ValueError(f"export parity shape_results[{index}]が不正です")
        try:
            shape_results.append(ExportParityShapeResult(**value))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"export parity shape_results[{index}]が不正です: {exc}"
            ) from exc
    values = dict(raw)
    values["sample_ids"] = tuple(sample_ids)
    values["training_sample_ids"] = tuple(training_sample_ids)
    values["sample_results"] = tuple(sample_results)
    values["shape_results"] = tuple(shape_results)
    try:
        report = ExportParityReport(**values)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"export parity report schemaが不正です: {exc}") from exc
    validate_export_parity_report(report)
    return report


def save_export_parity_report(path: Path, report: ExportParityReport) -> Path:
    """Persist a verified report with create-only semantics."""

    validate_export_parity_report(report)
    output = Path(path).expanduser().resolve()
    _atomic_json_new(output, report.to_dict())
    return output


def load_export_parity_report(path: Path) -> ExportParityReport:
    """Load a persisted export parity report with strict schema/hash checks."""

    return export_parity_report_from_dict(
        _read_json(Path(path).expanduser().resolve(strict=True))
    )


@attrs.frozen
class _LoadedWeights:
    model: Any
    lineage: ArtifactLineage
    run_kind: Literal["base-train", "finetune"]
    model_config: Mapping[str, Any]
    preprocess_schema: Mapping[str, Any]
    uncertainty_log_variance_offset: float


def export_onnx_model(
    weights_path: Path,
    output_path: Path,
    *,
    opset_version: int = DEFAULT_OPSET_VERSION,
    verification_shapes: Sequence[tuple[int, int]] = (
        (32, 32),
        (64, 128),
        (128, 64),
        (512, 512),
        (256, 1024),
    ),
    _formal_weights: FormalTrainingWeights | None = None,
) -> OnnxExportResult:
    """Low-level diagnostic export; release callers use the formal wrapper."""

    if opset_version < 18:
        raise ValueError("opset_versionは18以上が必要です")
    shapes = _validate_shapes(verification_shapes)
    weights = (
        load_export_weights(weights_path)
        if _formal_weights is None
        else validate_formal_training_weights(_formal_weights, weights_path)
    )
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"ONNX outputが既に存在します: {destination}")

    import torch
    from torch import nn

    class _ExportWrapper(nn.Module):
        def __init__(self, model: nn.Module, offset: float) -> None:
            super().__init__()
            self._model = model
            self._offset = offset

        @override
        def forward(
            self,
            image_6ch: torch.Tensor,
            valid_pixel_mask: torch.Tensor,
            pixel_per_mm: torch.Tensor,
        ) -> tuple[torch.Tensor, torch.Tensor]:
            mean, log_variance = self._model(image_6ch, valid_pixel_mask, pixel_per_mm)
            return mean, log_variance + self._offset

    wrapper = _ExportWrapper(
        weights.model, weights.uncertainty_log_variance_offset
    ).eval()
    example_height, example_width = (64, 64)
    example = (
        torch.zeros((1, 6, example_height, example_width), dtype=torch.float32),
        torch.ones((1, 1, example_height, example_width), dtype=torch.bool),
        torch.ones((1, 1), dtype=torch.float32) * 30.0,
    )
    height_dimension = torch.export.Dim("height", min=32, max=1024)
    width_dimension = torch.export.Dim("width", min=32, max=1024)
    try:
        export_dynamo_model(
            wrapper,
            example,
            destination,
            input_names=("image_6ch", "valid_pixel_mask", "pixel_per_mm"),
            output_names=("mean_volume_ul", "log_variance_volume_ul2"),
            dynamic_shapes=(
                {2: height_dimension, 3: width_dimension},
                {2: height_dimension, 3: width_dimension},
                None,
            ),
            opset_version=opset_version,
        )
        _write_onnx_export_metadata(
            destination,
            weights,
            weights_path=weights_path,
            formal_weights=_formal_weights,
        )
        node_types = check_onnx_model(destination)
        parity = validate_onnx_parity(
            weights_path,
            destination,
            shapes=shapes,
        )
        if not parity.passed:
            raise ValueError("exportしたONNX modelがeager parity gateを満たしません")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return OnnxExportResult(
        weights_path=Path(weights_path).expanduser().resolve(),
        model_path=destination,
        model_artifact_sha256=_sha256_file(destination),
        opset_version=opset_version,
        node_types=node_types,
        dynamic_shapes=shapes,
        lineage=weights.lineage,
        model_config=weights.model_config,
        preprocess_schema=weights.preprocess_schema,
        uncertainty_log_variance_offset=weights.uncertainty_log_variance_offset,
    )


def export_onnx_model_from_formal_weights(
    formal_weights: FormalTrainingWeights,
    output_path: Path,
    *,
    opset_version: int = DEFAULT_OPSET_VERSION,
    verification_shapes: Sequence[tuple[int, int]] = (
        (32, 32),
        (64, 128),
        (128, 64),
        (512, 512),
        (256, 1024),
    ),
) -> OnnxExportResult:
    """Export only from live-verified FINISHED base-train/finetune weights."""

    return export_onnx_model(
        formal_weights.weights_path,
        output_path,
        opset_version=opset_version,
        verification_shapes=verification_shapes,
        _formal_weights=formal_weights,
    )


def validate_onnx_parity(
    weights_path: Path,
    onnx_path: Path,
    *,
    shapes: Sequence[tuple[int, int]],
    seed: int = 1729,
) -> OnnxParityResult:
    """実ONNX Runtimeでdynamic shapeとeager出力のparityを検証する."""

    import torch

    checked_shapes = _validate_shapes(shapes)
    weights = load_export_weights(weights_path)
    model_path = Path(onnx_path).expanduser().resolve(strict=True)
    check_onnx_model(model_path)
    session = OnnxSession.load_cpu(model_path)
    generator = torch.Generator(device="cpu")
    generator.manual_seed(seed)
    maximum_mean_error = 0.0
    maximum_log_variance_error = 0.0
    passed = True
    weights.model.eval()
    with torch.no_grad():
        for height, width in checked_shapes:
            image = torch.randn(
                (1, 6, height, width), generator=generator, dtype=torch.float32
            )
            mask = torch.ones((1, 1, height, width), dtype=torch.bool)
            scale = torch.tensor([[30.0]], dtype=torch.float32)
            eager_mean, eager_log_variance = weights.model(image, mask, scale)
            eager_log_variance = (
                eager_log_variance + weights.uncertainty_log_variance_offset
            )
            actual_mean, actual_log_variance = session.run(
                ["mean_volume_ul", "log_variance_volume_ul2"],
                {
                    "image_6ch": image.numpy(),
                    "valid_pixel_mask": mask.numpy(),
                    "pixel_per_mm": scale.numpy(),
                },
            )
            eager_mean_value = float(eager_mean.item())
            eager_log_variance_value = float(eager_log_variance.item())
            actual_mean_value = float(np.asarray(actual_mean).item())
            actual_log_variance_value = float(np.asarray(actual_log_variance).item())
            values = (
                eager_mean_value,
                eager_log_variance_value,
                actual_mean_value,
                actual_log_variance_value,
            )
            if (
                not all(math.isfinite(value) for value in values)
                or actual_mean_value <= 0
            ):
                passed = False
                continue
            mean_parity, log_variance_parity = compare_eager_outputs(
                (eager_mean_value, eager_log_variance_value),
                (actual_mean_value, actual_log_variance_value),
                absolute_tolerances=(
                    FP32_ABSOLUTE_TOLERANCE_UL,
                    FP32_ABSOLUTE_TOLERANCE_UL,
                ),
                relative_tolerances=(
                    FP32_RELATIVE_TOLERANCE,
                    FP32_RELATIVE_TOLERANCE,
                ),
            )
            maximum_mean_error = max(maximum_mean_error, mean_parity.absolute_error)
            maximum_log_variance_error = max(
                maximum_log_variance_error,
                log_variance_parity.absolute_error,
            )
            passed = passed and mean_parity.passed and log_variance_parity.passed
    return OnnxParityResult(
        model_artifact_sha256=_sha256_file(model_path),
        shapes=checked_shapes,
        maximum_mean_absolute_error_ul=maximum_mean_error,
        maximum_log_variance_absolute_error=maximum_log_variance_error,
        passed=passed,
    )


def load_export_weights(weights_path: Path) -> _LoadedWeights:
    import torch

    from .model import PasteVolumeModelConfig, PasteVolumeResNet
    from .training_artifacts import load_model_weights

    path = Path(weights_path).expanduser().resolve(strict=True)
    try:
        raw = load_model_weights(path)
    except Exception as exc:
        raise ValueError(f"weights artifactを読めません: {exc}") from exc
    if raw["source_checkpoint_role"] != "best":
        raise ValueError("export source checkpoint roleはbestだけを許可します")
    if not isinstance(raw["model_config"], dict):
        raise ValueError("model_configはmappingが必要です")
    if not isinstance(raw["state_dict"], dict):
        raise ValueError("state_dictはmappingが必要です")
    if not isinstance(raw["preprocess_schema"], dict):
        raise ValueError("preprocess_schemaはmappingが必要です")
    _validate_canonical_preprocess_schema(raw["preprocess_schema"])
    offset = raw["uncertainty_log_variance_offset"]
    if not _is_finite_number(offset):
        raise ValueError("uncertainty_log_variance_offsetは有限値が必要です")
    parent_run = raw.get("parent_run_id")
    parent_checkpoint = raw.get("parent_checkpoint_id")
    source_run_id = _required_string(raw, "source_run_id")
    source_checkpoint_sha256 = _required_string(raw, "source_checkpoint_sha256")
    dataset_fingerprint = _required_string(raw, "dataset_fingerprint")
    split_fingerprint = _required_string(raw, "split_fingerprint")
    training_protocol_fingerprint = _required_string(
        raw, "training_protocol_fingerprint"
    )
    source_checkpoint_role = _required_best_role(raw)
    run_kind = raw.get("run_kind")
    if run_kind not in ("base-train", "finetune"):
        raise ValueError("weights run_kindが不正です")
    if parent_run is not None and (not isinstance(parent_run, str) or not parent_run):
        raise ValueError("parent_run_idが不正です")
    if parent_checkpoint is not None and (
        not isinstance(parent_checkpoint, str) or not parent_checkpoint
    ):
        raise ValueError("parent_checkpoint_idが不正です")
    try:
        config = PasteVolumeModelConfig(**raw["model_config"])
        model = PasteVolumeResNet(config)
        model.load_state_dict(raw["state_dict"], strict=True)
    except (TypeError, ValueError, RuntimeError) as exc:
        raise ValueError(f"weights model契約が不正です: {exc}") from exc
    for name, tensor in model.state_dict().items():
        if tensor.is_floating_point() and not torch.isfinite(tensor).all():
            raise ValueError(f"state_dictに非有限値があります: {name}")
    model.eval()
    return _LoadedWeights(
        model=model,
        lineage=ArtifactLineage(
            source_run_id=source_run_id,
            source_checkpoint_sha256=source_checkpoint_sha256,
            source_checkpoint_role=source_checkpoint_role,
            dataset_fingerprint=dataset_fingerprint,
            split_fingerprint=split_fingerprint,
            training_protocol_fingerprint=training_protocol_fingerprint,
            parent_run_id=parent_run,
            parent_checkpoint_id=parent_checkpoint,
        ),
        run_kind=cast(Literal["base-train", "finetune"], run_kind),
        model_config=cast(Mapping[str, Any], raw["model_config"]),
        preprocess_schema=cast(Mapping[str, Any], raw["preprocess_schema"]),
        uncertainty_log_variance_offset=float(offset),
    )


def validate_formal_training_weights(
    formal_weights: FormalTrainingWeights,
    weights_path: Path,
) -> _LoadedWeights:
    if not isinstance(formal_weights, FormalTrainingWeights):
        raise TypeError("formal training weightsはverified typed evidenceが必要です")
    path = Path(weights_path).expanduser().resolve(strict=True)
    if formal_weights.weights_path != path:
        raise ValueError("formal training weights pathが入力artifactと不一致です")
    actual_hash = "sha256:" + _sha256_file(path)
    if formal_weights.weights_sha256 != actual_hash:
        raise ValueError("formal training weights hashが入力artifactと不一致です")
    loaded = load_export_weights(path)
    checks = (
        (formal_weights.run_kind, loaded.run_kind),
        (formal_weights.source_run_id, loaded.lineage.source_run_id),
        (formal_weights.dataset_fingerprint, loaded.lineage.dataset_fingerprint),
        (formal_weights.split_fingerprint, loaded.lineage.split_fingerprint),
        (
            formal_weights.training_protocol_fingerprint,
            loaded.lineage.training_protocol_fingerprint,
        ),
    )
    if any(actual != expected for actual, expected in checks):
        raise ValueError("formal training weights lineageがstrict artifactと不一致です")
    attestation = formal_weights.attestation
    if (
        attestation.output_path != path
        or attestation.output_kind != "file"
        or attestation.output_fingerprint != actual_hash
        or attestation.run_id != loaded.lineage.source_run_id
        or attestation.run_kind != loaded.run_kind
        or attestation.status != "FINISHED"
    ):
        raise ValueError("formal training weights attestationがartifactと不一致です")
    return loaded


def check_onnx_model(path: Path) -> tuple[str, ...]:
    return inspect_onnx_model(
        path,
        allowed_operators=_ALLOWED_ONNX_OPERATORS,
    ).node_types


def _validate_model_format_graph(path: Path, model_format: ModelFormat) -> None:
    node_types = set(check_onnx_model(path))
    has_quantize = "QuantizeLinear" in node_types
    has_dequantize = "DequantizeLinear" in node_types
    if model_format == "onnx-fp32" and (has_quantize or has_dequantize):
        raise ValueError("onnx-fp32 model_formatとQ/DQ graphが一致しません")
    if model_format == "onnx-int8-qdq" and not (has_quantize and has_dequantize):
        raise ValueError("onnx-int8-qdq model_formatにはQ/DQ graphが必要です")


def validate_candidate_artifact(
    path: Path, model_format: ModelFormat
) -> Mapping[str, Any]:
    _validate_model_format_graph(path, model_format)
    metadata = read_onnx_export_metadata(path)
    expected_role: OnnxArtifactRole = (
        "optimized-fp32" if model_format == "onnx-fp32" else "int8-qdq"
    )
    if metadata.get("artifact_role") != expected_role:
        raise ValueError(
            f"{model_format} candidateにはartifact role {expected_role}が必要です"
        )
    source_export_hash = metadata.get("source_export_sha256")
    if not isinstance(source_export_hash, str) or not _is_sha256(source_export_hash):
        raise ValueError("candidateに元のFP32 export hashがありません")
    if source_export_hash == _sha256_file(path):
        raise ValueError("candidate自身を元のFP32 exportにできません")
    return metadata


def _validate_fp32_export_artifact(path: Path) -> Mapping[str, Any]:
    _validate_model_format_graph(path, "onnx-fp32")
    metadata = read_onnx_export_metadata(path)
    if metadata.get("artifact_role") != "export-fp32":
        raise ValueError("元のFP32 export artifactが必要です")
    return metadata


def validate_source_export_hash(
    metadata: Mapping[str, Any],
    *,
    candidate_artifact_sha256: str,
    fp32_reference_artifact_sha256: str,
) -> None:
    if not _is_sha256(fp32_reference_artifact_sha256):
        raise ValueError("FP32 reference artifact hashが不正です")
    if candidate_artifact_sha256 == fp32_reference_artifact_sha256:
        raise ValueError("candidate自身をFP32 referenceにできません")
    if metadata.get("source_export_sha256") != fp32_reference_artifact_sha256:
        raise ValueError("candidateが元のFP32 exportをreferenceにしていません")


def validate_candidate_reference_artifacts(
    candidate: Path, reference: Path, model_format: ModelFormat
) -> None:
    candidate_hash = _sha256_file(candidate)
    reference_hash = _sha256_file(reference)
    if candidate == reference:
        raise ValueError("candidate自身をFP32 referenceにできません")
    if candidate_hash == reference_hash:
        raise ValueError("candidateとFP32 referenceに同一artifactを指定できません")
    candidate_metadata = validate_candidate_artifact(candidate, model_format)
    _validate_fp32_export_artifact(reference)
    validate_source_export_hash(
        candidate_metadata,
        candidate_artifact_sha256=candidate_hash,
        fp32_reference_artifact_sha256=reference_hash,
    )


def optimize_model(
    onnx_model: Path,
    output_directory: Path,
    *,
    calibration_data: Sequence[Path],
    split_manifest: Path,
    calibration_sample_limit: int = 256,
    seed: int = 42,
) -> OptimizationResult:
    """Optimized FP32とtrain split限定のstatic INT8 QDQ候補を生成する.

    splitは再生成せず永続化済みmanifestを必須とし、composite fingerprintと
    全sample集合を照合した後、train assignmentだけからsession均等に選ぶ。
    """

    if type(calibration_sample_limit) is not int or calibration_sample_limit < 1:
        raise ValueError("calibration_sample_limitは1以上の整数が必要です")
    source = Path(onnx_model).expanduser().resolve(strict=True)
    metadata = _validate_fp32_export_artifact(source)
    lineage = _lineage_from_mapping(_required_mapping(metadata, "lineage"))
    preprocess_schema = _required_mapping(metadata, "preprocess_schema")
    constraints = _image_constraints_from_schema(preprocess_schema)
    split_path = Path(split_manifest).expanduser().resolve(strict=True)
    roots = tuple(Path(path).expanduser().resolve() for path in calibration_data)
    if not roots:
        raise ValueError("calibration_dataを1件以上指定してください")

    from .data import (
        build_sample_index,
        load_preprocessed_sample,
        load_split_manifest,
        resolve_dataset_inputs,
        sample_index_fingerprint,
        select_session_balanced_samples,
        validate_split_manifest,
    )

    if len(roots) == 1 and roots[0].is_file():
        composite = resolve_dataset_inputs(manifest=roots[0])
    else:
        composite = resolve_dataset_inputs(roots=roots)
    samples = build_sample_index(composite)
    split = load_split_manifest(
        split_path,
        expected_composite_fingerprint=composite.composite_fingerprint,
    )
    validate_split_manifest(
        split,
        samples,
        expected_composite_fingerprint=composite.composite_fingerprint,
    )
    if composite.composite_fingerprint != lineage.dataset_fingerprint:
        raise ValueError(
            "calibration datasetがtraining dataset fingerprintと不一致です"
        )
    if split.split_fingerprint != lineage.split_fingerprint:
        raise ValueError("persisted splitがtraining split fingerprintと不一致です")
    selected = select_session_balanced_samples(
        samples,
        split.train_sample_ids,
        limit=calibration_sample_limit,
        seed=seed,
    )
    if not selected:
        raise ValueError("train splitにquantization calibration sampleがありません")

    feeds: list[dict[str, np.ndarray[Any, Any]]] = []
    for sample in selected:
        processed = load_preprocessed_sample(
            sample,
            constraints=constraints,
            training=False,
        )
        feeds.append(
            {
                "image_6ch": processed.image_6ch.detach()
                .cpu()
                .float()
                .numpy()[None, ...],
                "valid_pixel_mask": processed.valid_pixel_mask.detach()
                .cpu()
                .numpy()[None, ...]
                .astype(np.bool_),
                "pixel_per_mm": np.asarray(
                    [[processed.pixel_per_mm]], dtype=np.float32
                ),
            }
        )

    destination = Path(output_directory).expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"optimization outputが既に存在します: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    try:
        optimized_path = temporary / "model.optimized.fp32.onnx"
        int8_path = temporary / "model.int8.qdq.onnx"
        optimize_fp32(source, optimized_path)
        _copy_onnx_export_metadata(
            source, optimized_path, artifact_role="optimized-fp32"
        )
        quantize_static_qdq(optimized_path, int8_path, feeds)
        _copy_onnx_export_metadata(source, int8_path, artifact_role="int8-qdq")
        check_onnx_model(optimized_path)
        check_onnx_model(int8_path)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    final_fp32 = destination / "model.optimized.fp32.onnx"
    final_int8 = destination / "model.int8.qdq.onnx"
    return OptimizationResult(
        source_model_path=source,
        source_model_artifact_sha256=_sha256_file(source),
        optimized_fp32_path=final_fp32,
        optimized_fp32_artifact_sha256=_sha256_file(final_fp32),
        int8_path=final_int8,
        int8_artifact_sha256=_sha256_file(final_int8),
        calibration_sample_ids=tuple(sample.sample_id for sample in selected),
        calibration_session_ids=tuple(sample.session_id for sample in selected),
        calibration_sample_index_fingerprint=sample_index_fingerprint(samples),
        split_manifest_path=split_path,
        lineage=lineage,
    )


def _write_onnx_export_metadata(
    path: Path,
    weights: _LoadedWeights,
    *,
    weights_path: Path,
    formal_weights: FormalTrainingWeights | None,
) -> None:
    import torch

    opset_version = standard_opset_version(path)
    metadata = {
        "kind": "pcbasm-paste-volume-onnx-export",
        "schema_version": 1,
        "artifact_role": "export-fp32",
        "training_weights_attestation": (
            formal_weights.attestation.to_dict() if formal_weights is not None else None
        ),
        "source_weights_sha256": "sha256:"
        + _sha256_file(Path(weights_path).expanduser().resolve(strict=True)),
        "lineage": weights.lineage.to_dict(),
        "model_config": dict(weights.model_config),
        "preprocess_schema": dict(weights.preprocess_schema),
        "uncertainty_log_variance_offset": weights.uncertainty_log_variance_offset,
        "export_provenance": {
            "api": "torch.onnx.export",
            "mode": "dynamo",
            "torch_version": torch.__version__,
            "onnx_version": onnx_version(),
            "opset_version": opset_version,
        },
        "artifact_provenance": {
            "role": "export-fp32",
            "tool": "torch.onnx.export",
            "version": torch.__version__,
        },
    }
    write_json_metadata(
        path,
        key="pcbasm.paste_volume.export",
        value=metadata,
        clear_existing=True,
    )


def read_onnx_export_metadata(path: Path) -> Mapping[str, Any]:
    try:
        value = read_json_metadata(path, key="pcbasm.paste_volume.export")
    except ValueError as exc:
        raise ValueError("ONNX training lineage metadataが不正です") from exc
    if value.get("kind") != "pcbasm-paste-volume-onnx-export":
        raise ValueError("ONNX training lineage metadata kindが不正です")
    if value.get("schema_version") != 1:
        raise ValueError("未知のONNX training lineage metadata schemaです")
    artifact_role = value.get("artifact_role")
    base_keys = {
        "kind",
        "schema_version",
        "artifact_role",
        "training_weights_attestation",
        "source_weights_sha256",
        "lineage",
        "model_config",
        "preprocess_schema",
        "uncertainty_log_variance_offset",
        "export_provenance",
        "artifact_provenance",
    }
    expected_keys = (
        base_keys
        if artifact_role == "export-fp32"
        else base_keys | {"source_export_sha256"}
    )
    if artifact_role not in {"export-fp32", "optimized-fp32", "int8-qdq"}:
        raise ValueError("ONNX artifact roleが不正です")
    if set(value) != expected_keys:
        raise ValueError("ONNX training lineage metadata key集合が不正です")
    source_weights_sha256 = value.get("source_weights_sha256")
    if not isinstance(source_weights_sha256, str) or not _is_prefixed_sha256(
        source_weights_sha256
    ):
        raise ValueError("ONNX source weights fingerprintが不正です")
    lineage = _lineage_from_mapping(_required_mapping(value, "lineage"))
    training_attestation = value.get("training_weights_attestation")
    if training_attestation is not None:
        if not isinstance(training_attestation, Mapping):
            raise ValueError("ONNX training weights attestationが不正です")
        try:
            attestation = FormalArtifactAttestation.from_dict(training_attestation)
        except ValueError as exc:
            raise ValueError("ONNX training weights attestationが不正です") from exc
        if (
            attestation.output_kind != "file"
            or attestation.output_fingerprint != source_weights_sha256
            or attestation.run_id != lineage.source_run_id
            or attestation.run_kind not in ("base-train", "finetune")
        ):
            raise ValueError("ONNX training weights attestationがlineageと不一致です")
    if artifact_role != "export-fp32":
        source_export = value.get("source_export_sha256")
        if not isinstance(source_export, str) or not _is_sha256(source_export):
            raise ValueError("ONNX source export fingerprintが不正です")
    offset = value.get("uncertainty_log_variance_offset")
    if not _is_finite_number(offset):
        raise ValueError("ONNX uncertainty calibration offsetが不正です")
    export_provenance = _required_mapping(value, "export_provenance")
    if set(export_provenance) != {
        "api",
        "mode",
        "torch_version",
        "onnx_version",
        "opset_version",
    }:
        raise ValueError("ONNX export provenance key集合が不正です")
    if (
        export_provenance.get("api") != "torch.onnx.export"
        or export_provenance.get("mode") != "dynamo"
        or not isinstance(export_provenance.get("torch_version"), str)
        or not export_provenance["torch_version"]
        or not isinstance(export_provenance.get("onnx_version"), str)
        or not export_provenance["onnx_version"]
        or type(export_provenance.get("opset_version")) is not int
        or int(export_provenance["opset_version"]) < 18
    ):
        raise ValueError("ONNX export provenanceが不正です")
    if export_provenance["opset_version"] != standard_opset_version(path):
        raise ValueError("ONNX export provenanceのopsetがgraphと不一致です")
    artifact_provenance = _required_mapping(value, "artifact_provenance")
    if set(artifact_provenance) != {"role", "tool", "version"}:
        raise ValueError("ONNX artifact provenance key集合が不正です")
    expected_tool = (
        "torch.onnx.export" if artifact_role == "export-fp32" else "onnxruntime"
    )
    if (
        artifact_provenance.get("role") != artifact_role
        or artifact_provenance.get("tool") != expected_tool
        or not isinstance(artifact_provenance.get("version"), str)
        or not artifact_provenance["version"]
    ):
        raise ValueError("ONNX artifact provenanceが不正です")
    return cast(Mapping[str, Any], value)


def _copy_onnx_export_metadata(
    source: Path,
    destination: Path,
    *,
    artifact_role: Literal["optimized-fp32", "int8-qdq"],
) -> None:
    source_metadata = read_onnx_export_metadata(source)
    if source_metadata.get("artifact_role") != "export-fp32":
        raise ValueError("optimization sourceは元のFP32 exportが必要です")
    metadata = {
        **source_metadata,
        "artifact_role": artifact_role,
        "source_export_sha256": _sha256_file(source),
        "artifact_provenance": {
            "role": artifact_role,
            "tool": "onnxruntime",
            "version": onnxruntime_version(),
        },
    }
    write_json_metadata(
        destination,
        key="pcbasm.paste_volume.export",
        value=metadata,
    )


def _validate_shapes(
    shapes: Sequence[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    checked: list[tuple[int, int]] = []
    for shape in shapes:
        if len(shape) != 2 or any(
            type(value) is not int or value < 32 for value in shape
        ):
            raise ValueError("verification shapeは32以上の(H, W)が必要です")
        height, width = shape
        if max(height, width) > 1024 or height * width > 262_144:
            raise ValueError("verification shapeが画像制約を超えています")
        checked.append((height, width))
    if not checked:
        raise ValueError("verification shapeが必要です")
    return tuple(checked)
