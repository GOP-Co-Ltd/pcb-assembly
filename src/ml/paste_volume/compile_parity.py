"""Persistable eager/``torch.compile`` parity evidence for model release."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

import torch
from torch import Tensor, nn

from ml.evaluation.compile_parity import (
    CompileStepComparison as _ParityComparison,
    CompileStepResult as _StepResult,
    compare_compile_steps as _compare_steps,
    execute_compile_step,
    prepare_compile_models,
    resolve_compile_device,
    torch_compile_graph_break_count,
)
from ml.model.regression import weighted_gaussian_nll

COMPILE_PARITY_KIND = "pcbasm-paste-volume-compile-parity"
COMPILE_PARITY_SCHEMA_VERSION = 1
COMPILE_PARITY_TOLERANCE_PROFILE = "fp32-v1"

type CompileShapeCase = Literal[
    "minimum",
    "maximum-area",
    "portrait",
    "landscape",
    "training-batch",
]

REQUIRED_COMPILE_SHAPE_CASES: tuple[CompileShapeCase, ...] = (
    "minimum",
    "maximum-area",
    "portrait",
    "landscape",
    "training-batch",
)

_FIXED_SHAPES: Mapping[CompileShapeCase, tuple[int, int]] = {
    "minimum": (32, 32),
    "maximum-area": (512, 512),
    "portrait": (1024, 256),
    "landscape": (256, 1024),
}
_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class CompileParityConfig:
    """Compiler selection and the versioned FP32 comparison tolerance."""

    backend: str = "inductor"
    mode: str = "default"
    device: Literal["auto", "cpu", "cuda"] = "auto"
    dtype: Literal["float32"] = "float32"
    fullgraph: bool = False
    dynamic: bool | None = None
    rtol: float = 1e-3
    atol: float = 1e-5

    def __post_init__(self) -> None:
        if not self.backend.strip() or not self.mode.strip():
            raise ValueError("compile backend and mode must be non-empty")
        if self.device not in ("auto", "cpu", "cuda"):
            raise ValueError("compile parity device must be auto, cpu, or cuda")
        if self.dtype != "float32":
            raise ValueError("compile parity schema v1 supports only float32")
        if (
            not math.isfinite(self.rtol)
            or not math.isfinite(self.atol)
            or self.rtol < 0
            or self.atol < 0
            or self.rtol + self.atol <= 0
        ):
            raise ValueError(
                "compile parity tolerances must be finite and non-negative"
            )


@dataclass(frozen=True)
class CompileParityBatch:
    """One release shape case with the same tensors used by the training
    loss."""

    case_id: str
    shape_case: CompileShapeCase
    image_6ch: Tensor
    valid_pixel_mask: Tensor
    pixel_per_mm: Tensor
    target_volume_ul: Tensor
    sample_weight: Tensor
    sample_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("compile parity case_id must be non-empty")
        if self.shape_case not in REQUIRED_COMPILE_SHAPE_CASES:
            raise ValueError(
                f"unsupported compile parity shape case: {self.shape_case}"
            )
        if self.image_6ch.ndim != 4 or self.image_6ch.shape[1] != 6:
            raise ValueError("image_6ch must have shape [B, 6, H, W]")
        batch_size, _, height, width = self.image_6ch.shape
        if batch_size < 1 or height < 1 or width < 1:
            raise ValueError("compile parity batch dimensions must be positive")
        expected_fixed_shape = _FIXED_SHAPES.get(self.shape_case)
        if expected_fixed_shape is not None and (height, width) != expected_fixed_shape:
            raise ValueError(
                f"{self.shape_case} compile case requires HxW={expected_fixed_shape}"
            )
        if self.valid_pixel_mask.shape != (batch_size, 1, height, width):
            raise ValueError("valid_pixel_mask must have shape [B, 1, H, W]")
        if self.valid_pixel_mask.dtype is not torch.bool:
            raise ValueError("valid_pixel_mask must be a boolean tensor")
        for name, tensor in (
            ("pixel_per_mm", self.pixel_per_mm),
            ("target_volume_ul", self.target_volume_ul),
            ("sample_weight", self.sample_weight),
        ):
            if tensor.shape != (batch_size, 1) or not tensor.is_floating_point():
                raise ValueError(f"{name} must be a floating [B, 1] tensor")
        if not self.image_6ch.is_floating_point():
            raise ValueError("image_6ch must be floating point")
        tensors = (
            self.image_6ch,
            self.pixel_per_mm,
            self.target_volume_ul,
            self.sample_weight,
        )
        if not all(torch.all(torch.isfinite(tensor)).item() for tensor in tensors):
            raise ValueError("compile parity tensors must contain only finite values")
        if torch.any(self.pixel_per_mm <= 0).item():
            raise ValueError("pixel_per_mm must be positive")
        if (
            torch.any(self.sample_weight < 0).item()
            or self.sample_weight.sum().item() <= 0
        ):
            raise ValueError("sample_weight must be non-negative with a positive sum")
        if not torch.any(self.valid_pixel_mask).item():
            raise ValueError("valid_pixel_mask must contain at least one valid pixel")
        if len(self.sample_ids) != batch_size:
            raise ValueError("sample_ids and tensor batch size must match")
        if any(not sample_id.strip() for sample_id in self.sample_ids):
            raise ValueError("compile parity sample_ids must be non-empty")
        if len(set(self.sample_ids)) != len(self.sample_ids):
            raise ValueError("compile parity sample_ids must be unique within a case")


@dataclass(frozen=True)
class CompileParityProvenance:
    weights_sha256: str
    training_protocol_fingerprint: str
    dataset_fingerprint: str
    split_fingerprint: str

    def __post_init__(self) -> None:
        for name, value in (
            ("weights_sha256", self.weights_sha256),
            ("training_protocol_fingerprint", self.training_protocol_fingerprint),
            ("dataset_fingerprint", self.dataset_fingerprint),
            ("split_fingerprint", self.split_fingerprint),
        ):
            if not _SHA256_PATTERN.fullmatch(value):
                raise ValueError(f"{name} must be an exact sha256 fingerprint")

    def to_dict(self) -> dict[str, str]:
        return {
            "weights_sha256": self.weights_sha256,
            "training_protocol_fingerprint": self.training_protocol_fingerprint,
            "dataset_fingerprint": self.dataset_fingerprint,
            "split_fingerprint": self.split_fingerprint,
        }


@dataclass(frozen=True)
class CompileParityRuntime:
    backend: str
    mode: str
    device: str
    dtype: str
    torch_version: str
    tolerance_profile: str
    rtol: float
    atol: float
    fullgraph: bool
    dynamic: bool | None
    compile_setup_seconds: float
    graph_break_count: int

    def __post_init__(self) -> None:
        if not self.backend.strip() or not self.mode.strip():
            raise ValueError("compile runtime backend and mode must be non-empty")
        if not re.fullmatch(r"(?:cpu|cuda)(?::[0-9]+)?", self.device):
            raise ValueError(
                "compile runtime device must be a resolved CPU/CUDA device"
            )
        if self.dtype != "float32":
            raise ValueError("compile parity runtime dtype must be float32")
        if not self.torch_version.strip():
            raise ValueError("torch_version must be non-empty")
        if self.tolerance_profile != COMPILE_PARITY_TOLERANCE_PROFILE:
            raise ValueError("unsupported compile parity tolerance profile")
        for name, value in (
            ("rtol", self.rtol),
            ("atol", self.atol),
            ("compile_setup_seconds", self.compile_setup_seconds),
        ):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.rtol + self.atol <= 0:
            raise ValueError("at least one compile parity tolerance must be positive")
        if type(self.graph_break_count) is not int or self.graph_break_count < 0:
            raise ValueError("graph_break_count must be a non-negative integer")

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "mode": self.mode,
            "device": self.device,
            "dtype": self.dtype,
            "torch_version": self.torch_version,
            "tolerance_profile": self.tolerance_profile,
            "rtol": self.rtol,
            "atol": self.atol,
            "fullgraph": self.fullgraph,
            "dynamic": self.dynamic,
            "compile_setup_seconds": self.compile_setup_seconds,
            "graph_break_count": self.graph_break_count,
        }


@dataclass(frozen=True)
class CompileParityCaseResult:
    case_id: str
    shape_case: CompileShapeCase
    input_shape: tuple[int, int, int, int]
    sample_ids: tuple[str, ...]
    compile_succeeded: bool
    parity_passed: bool
    trainable_parameter_count: int
    checked_gradient_count: int
    missing_gradient_parameters: tuple[str, ...]
    nonfinite_gradient_parameters: tuple[str, ...]
    mismatched_gradient_parameters: tuple[str, ...]
    eager_step_seconds: float | None
    compile_and_first_step_seconds: float | None
    compiled_replay_step_seconds: float | None
    eager_samples_per_second: float | None
    compiled_samples_per_second: float | None
    mean_max_abs_error: float | None
    mean_max_relative_error: float | None
    log_variance_max_abs_error: float | None
    log_variance_max_relative_error: float | None
    loss_max_abs_error: float | None
    loss_max_relative_error: float | None
    gradient_max_abs_error: float | None
    gradient_max_relative_error: float | None
    failure_reason: str | None

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("compile parity result case_id must be non-empty")
        if self.shape_case not in REQUIRED_COMPILE_SHAPE_CASES:
            raise ValueError("compile parity result has an unsupported shape case")
        if len(self.input_shape) != 4 or any(
            type(value) is not int or value < 1 for value in self.input_shape
        ):
            raise ValueError(
                "compile parity input_shape must contain four positive ints"
            )
        if self.input_shape[1] != 6:
            raise ValueError("compile parity input_shape must contain six channels")
        expected_fixed_shape = _FIXED_SHAPES.get(self.shape_case)
        if (
            expected_fixed_shape is not None
            and self.input_shape[2:] != expected_fixed_shape
        ):
            raise ValueError(
                "compile parity result does not match its fixed shape case"
            )
        if len(self.sample_ids) != self.input_shape[0]:
            raise ValueError(
                "compile parity result sample count does not match batch size"
            )
        if any(not sample_id.strip() for sample_id in self.sample_ids):
            raise ValueError("compile parity result sample_ids must be non-empty")
        if len(set(self.sample_ids)) != len(self.sample_ids):
            raise ValueError("compile parity result sample_ids must be unique")
        if self.trainable_parameter_count < 1:
            raise ValueError("compile parity requires trainable parameters")
        if not 0 <= self.checked_gradient_count <= self.trainable_parameter_count:
            raise ValueError("checked_gradient_count is outside the parameter count")
        for values in (
            self.missing_gradient_parameters,
            self.nonfinite_gradient_parameters,
            self.mismatched_gradient_parameters,
        ):
            if any(not value.strip() for value in values) or len(set(values)) != len(
                values
            ):
                raise ValueError(
                    "gradient parameter lists must be non-empty and unique"
                )
        optional_numbers = (
            self.eager_step_seconds,
            self.compile_and_first_step_seconds,
            self.compiled_replay_step_seconds,
            self.eager_samples_per_second,
            self.compiled_samples_per_second,
            self.mean_max_abs_error,
            self.mean_max_relative_error,
            self.log_variance_max_abs_error,
            self.log_variance_max_relative_error,
            self.loss_max_abs_error,
            self.loss_max_relative_error,
            self.gradient_max_abs_error,
            self.gradient_max_relative_error,
        )
        if any(
            value is not None and (not math.isfinite(value) or value < 0)
            for value in optional_numbers
        ):
            raise ValueError(
                "compile parity measurements must be finite and non-negative"
            )
        if self.parity_passed and not self.compile_succeeded:
            raise ValueError("parity cannot pass when compilation failed")
        if self.parity_passed:
            if self.failure_reason is not None:
                raise ValueError("a passing compile parity case cannot have a failure")
            if self.checked_gradient_count != self.trainable_parameter_count:
                raise ValueError("a passing case must check every trainable gradient")
            if any(
                (
                    self.missing_gradient_parameters,
                    self.nonfinite_gradient_parameters,
                    self.mismatched_gradient_parameters,
                )
            ):
                raise ValueError("a passing case cannot contain gradient failures")
            if any(value is None for value in optional_numbers):
                raise ValueError("a passing case requires all parity measurements")
        elif self.failure_reason is None or not self.failure_reason.strip():
            raise ValueError("a failing compile parity case requires a failure_reason")

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "shape_case": self.shape_case,
            "input_shape": list(self.input_shape),
            "sample_ids": list(self.sample_ids),
            "compile_succeeded": self.compile_succeeded,
            "parity_passed": self.parity_passed,
            "trainable_parameter_count": self.trainable_parameter_count,
            "checked_gradient_count": self.checked_gradient_count,
            "missing_gradient_parameters": list(self.missing_gradient_parameters),
            "nonfinite_gradient_parameters": list(self.nonfinite_gradient_parameters),
            "mismatched_gradient_parameters": list(self.mismatched_gradient_parameters),
            "eager_step_seconds": self.eager_step_seconds,
            "compile_and_first_step_seconds": self.compile_and_first_step_seconds,
            "compiled_replay_step_seconds": self.compiled_replay_step_seconds,
            "eager_samples_per_second": self.eager_samples_per_second,
            "compiled_samples_per_second": self.compiled_samples_per_second,
            "mean_max_abs_error": self.mean_max_abs_error,
            "mean_max_relative_error": self.mean_max_relative_error,
            "log_variance_max_abs_error": self.log_variance_max_abs_error,
            "log_variance_max_relative_error": self.log_variance_max_relative_error,
            "loss_max_abs_error": self.loss_max_abs_error,
            "loss_max_relative_error": self.loss_max_relative_error,
            "gradient_max_abs_error": self.gradient_max_abs_error,
            "gradient_max_relative_error": self.gradient_max_relative_error,
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True)
class CompileParityReport:
    kind: str
    schema_version: int
    created_at_utc: str
    success: bool
    provenance: CompileParityProvenance
    runtime: CompileParityRuntime
    cases: tuple[CompileParityCaseResult, ...]
    content_sha256: str

    def __post_init__(self) -> None:
        if self.kind != COMPILE_PARITY_KIND:
            raise ValueError("unsupported compile parity report kind")
        if self.schema_version != COMPILE_PARITY_SCHEMA_VERSION:
            raise ValueError("unsupported compile parity report schema version")
        try:
            created_at = datetime.fromisoformat(self.created_at_utc)
        except ValueError as error:
            raise ValueError("created_at_utc must be an ISO-8601 timestamp") from error
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("created_at_utc must include a timezone")
        if created_at.utcoffset() != timedelta(0):
            raise ValueError("created_at_utc must use UTC")
        if not self.cases:
            raise ValueError("compile parity report cases must be non-empty")
        case_ids = tuple(result.case_id for result in self.cases)
        shape_cases = tuple(result.shape_case for result in self.cases)
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("compile parity report contains duplicate case_id")
        if len(set(shape_cases)) != len(shape_cases):
            raise ValueError("compile parity report contains duplicate shape cases")
        if set(shape_cases) != set(REQUIRED_COMPILE_SHAPE_CASES):
            raise ValueError(
                "compile parity report must contain every required shape case"
            )
        expected_success = self.runtime.graph_break_count == 0 and all(
            result.compile_succeeded and result.parity_passed for result in self.cases
        )
        if self.success != expected_success:
            raise ValueError(
                "compile parity report success does not match case results"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "created_at_utc": self.created_at_utc,
            "success": self.success,
            "provenance": self.provenance.to_dict(),
            "runtime": self.runtime.to_dict(),
            "cases": [result.to_dict() for result in self.cases],
            "content_sha256": self.content_sha256,
        }


def _sha256_file(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"weights artifact is not a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _content_sha256(report: CompileParityReport) -> str:
    content = report.to_dict()
    del content["content_sha256"]
    return "sha256:" + hashlib.sha256(_canonical_json(content)).hexdigest()


def _finalize_report(report: CompileParityReport) -> CompileParityReport:
    if report.content_sha256:
        raise ValueError("cannot finalize an already-hashed compile parity report")
    return replace(report, content_sha256=_content_sha256(report))


def validate_compile_parity_report(report: CompileParityReport) -> None:
    """Validate the complete report, including its canonical content hash."""

    if not _SHA256_PATTERN.fullmatch(report.content_sha256):
        raise ValueError("compile parity content_sha256 must be an exact fingerprint")
    if report.content_sha256 != _content_sha256(report):
        raise ValueError("compile parity content_sha256 does not match report content")


def compile_parity_report_sha256(report: CompileParityReport) -> str:
    """Return the verified canonical content fingerprint."""

    validate_compile_parity_report(report)
    return report.content_sha256


def _move_batch(
    batch: CompileParityBatch, device: torch.device
) -> tuple[Tensor, Tensor, Tensor, Tensor, Tensor]:
    return (
        batch.image_6ch.to(device=device, dtype=torch.float32),
        batch.valid_pixel_mask.to(device=device),
        batch.pixel_per_mm.to(device=device, dtype=torch.float32),
        batch.target_volume_ul.to(device=device, dtype=torch.float32),
        batch.sample_weight.to(device=device, dtype=torch.float32),
    )


def _execute_step(
    model: nn.Module,
    inputs: tuple[Tensor, Tensor, Tensor, Tensor, Tensor],
    *,
    device: torch.device,
    gradient_model: nn.Module | None = None,
) -> _StepResult:
    image, valid_mask, pixel_per_mm, target, sample_weight = inputs
    return execute_compile_step(
        model,
        (image, valid_mask, pixel_per_mm),
        target=target,
        sample_weight=sample_weight,
        loss_function=weighted_gaussian_nll,
        device=device,
        gradient_model=gradient_model,
    )


def _failure_reason(stage: str, error: Exception) -> str:
    detail = str(error).strip() or "no error detail"
    return f"{stage}: {type(error).__name__}: {detail}"[:4000]


def _failed_case(
    batch: CompileParityBatch,
    *,
    trainable_parameter_count: int,
    reason: str,
    compile_succeeded: bool = False,
    eager_step: _StepResult | None = None,
    compiled_step: _StepResult | None = None,
    comparison: _ParityComparison | None = None,
) -> CompileParityCaseResult:
    batch_size = batch.image_6ch.shape[0]
    return CompileParityCaseResult(
        case_id=batch.case_id,
        shape_case=batch.shape_case,
        input_shape=cast(tuple[int, int, int, int], tuple(batch.image_6ch.shape)),
        sample_ids=batch.sample_ids,
        compile_succeeded=compile_succeeded,
        parity_passed=False,
        trainable_parameter_count=trainable_parameter_count,
        checked_gradient_count=(
            comparison.checked_gradient_count if comparison is not None else 0
        ),
        missing_gradient_parameters=(
            comparison.missing_gradient_parameters if comparison is not None else ()
        ),
        nonfinite_gradient_parameters=(
            comparison.nonfinite_gradient_parameters if comparison is not None else ()
        ),
        mismatched_gradient_parameters=(
            comparison.mismatched_gradient_parameters if comparison is not None else ()
        ),
        eager_step_seconds=eager_step.seconds if eager_step is not None else None,
        compile_and_first_step_seconds=(
            compiled_step.seconds if compiled_step is not None else None
        ),
        compiled_replay_step_seconds=None,
        eager_samples_per_second=(
            batch_size / eager_step.seconds if eager_step is not None else None
        ),
        compiled_samples_per_second=None,
        mean_max_abs_error=comparison.mean.max_abs if comparison is not None else None,
        mean_max_relative_error=(
            comparison.mean.max_relative if comparison is not None else None
        ),
        log_variance_max_abs_error=(
            comparison.log_variance.max_abs if comparison is not None else None
        ),
        log_variance_max_relative_error=(
            comparison.log_variance.max_relative if comparison is not None else None
        ),
        loss_max_abs_error=comparison.loss.max_abs if comparison is not None else None,
        loss_max_relative_error=(
            comparison.loss.max_relative if comparison is not None else None
        ),
        gradient_max_abs_error=(
            comparison.gradient_max_abs if comparison is not None else None
        ),
        gradient_max_relative_error=(
            comparison.gradient_max_relative if comparison is not None else None
        ),
        failure_reason=reason,
    )


def _run_case(
    eager_model: nn.Module,
    compiled_model: nn.Module,
    compiled_source: nn.Module,
    batch: CompileParityBatch,
    *,
    device: torch.device,
    config: CompileParityConfig,
    trainable_parameter_count: int,
) -> CompileParityCaseResult:
    inputs = _move_batch(batch, device)
    try:
        eager_step = _execute_step(eager_model, inputs, device=device)
    except Exception as error:
        return _failed_case(
            batch,
            trainable_parameter_count=trainable_parameter_count,
            reason=_failure_reason("eager step failed", error),
        )
    try:
        compiled_step = _execute_step(
            compiled_model,
            inputs,
            device=device,
            gradient_model=compiled_source,
        )
    except Exception as error:
        return _failed_case(
            batch,
            trainable_parameter_count=trainable_parameter_count,
            reason=_failure_reason("compiled first step failed", error),
            eager_step=eager_step,
        )
    try:
        comparison = _compare_steps(
            eager_step,
            compiled_step,
            rtol=config.rtol,
            atol=config.atol,
        )
    except Exception as error:
        return _failed_case(
            batch,
            trainable_parameter_count=trainable_parameter_count,
            reason=_failure_reason("parity comparison failed", error),
            compile_succeeded=True,
            eager_step=eager_step,
            compiled_step=compiled_step,
        )
    try:
        compiled_replay = _execute_step(
            compiled_model,
            inputs,
            device=device,
            gradient_model=compiled_source,
        )
    except Exception as error:
        return _failed_case(
            batch,
            trainable_parameter_count=trainable_parameter_count,
            reason=_failure_reason("compiled replay step failed", error),
            eager_step=eager_step,
            compiled_step=compiled_step,
            comparison=comparison,
        )
    if not comparison.passed:
        failures: list[str] = []
        if not comparison.mean.close:
            failures.append("mean")
        if not comparison.log_variance.close:
            failures.append("log_variance")
        if not comparison.loss.close:
            failures.append("weighted_gaussian_nll")
        if comparison.missing_gradient_parameters:
            failures.append("missing_gradients")
        if comparison.nonfinite_gradient_parameters:
            failures.append("nonfinite_gradients")
        if comparison.mismatched_gradient_parameters:
            failures.append("mismatched_gradients")
        return _failed_case(
            batch,
            trainable_parameter_count=trainable_parameter_count,
            reason="parity tolerance exceeded: " + ", ".join(failures),
            compile_succeeded=True,
            eager_step=eager_step,
            compiled_step=compiled_step,
            comparison=comparison,
        )
    batch_size = batch.image_6ch.shape[0]
    return CompileParityCaseResult(
        case_id=batch.case_id,
        shape_case=batch.shape_case,
        input_shape=cast(tuple[int, int, int, int], tuple(batch.image_6ch.shape)),
        sample_ids=batch.sample_ids,
        compile_succeeded=True,
        parity_passed=True,
        trainable_parameter_count=trainable_parameter_count,
        checked_gradient_count=comparison.checked_gradient_count,
        missing_gradient_parameters=(),
        nonfinite_gradient_parameters=(),
        mismatched_gradient_parameters=(),
        eager_step_seconds=eager_step.seconds,
        compile_and_first_step_seconds=compiled_step.seconds,
        compiled_replay_step_seconds=compiled_replay.seconds,
        eager_samples_per_second=batch_size / eager_step.seconds,
        compiled_samples_per_second=batch_size / compiled_replay.seconds,
        mean_max_abs_error=comparison.mean.max_abs,
        mean_max_relative_error=comparison.mean.max_relative,
        log_variance_max_abs_error=comparison.log_variance.max_abs,
        log_variance_max_relative_error=comparison.log_variance.max_relative,
        loss_max_abs_error=comparison.loss.max_abs,
        loss_max_relative_error=comparison.loss.max_relative,
        gradient_max_abs_error=comparison.gradient_max_abs,
        gradient_max_relative_error=comparison.gradient_max_relative,
        failure_reason=None,
    )


def run_compile_parity(
    model: nn.Module,
    batches: Sequence[CompileParityBatch],
    *,
    weights_path: Path,
    training_protocol_fingerprint: str,
    dataset_fingerprint: str,
    split_fingerprint: str,
    config: CompileParityConfig | None = None,
) -> CompileParityReport:
    """Run all mandatory release shapes and return hashed success/failure
    evidence."""

    resolved_config = config or CompileParityConfig()
    by_shape: dict[CompileShapeCase, CompileParityBatch] = {}
    case_ids: set[str] = set()
    for batch in batches:
        if batch.case_id in case_ids:
            raise ValueError(f"duplicate compile parity case_id: {batch.case_id}")
        if batch.shape_case in by_shape:
            raise ValueError(f"duplicate compile parity shape case: {batch.shape_case}")
        case_ids.add(batch.case_id)
        by_shape[batch.shape_case] = batch
    missing = set(REQUIRED_COMPILE_SHAPE_CASES) - set(by_shape)
    if missing:
        raise ValueError(f"missing compile parity shape cases: {sorted(missing)}")

    provenance = CompileParityProvenance(
        weights_sha256=_sha256_file(weights_path),
        training_protocol_fingerprint=training_protocol_fingerprint,
        dataset_fingerprint=dataset_fingerprint,
        split_fingerprint=split_fingerprint,
    )
    device = resolve_compile_device(resolved_config.device)
    prepared_models = prepare_compile_models(
        model,
        device=device,
        backend=resolved_config.backend,
        mode=resolved_config.mode,
        fullgraph=resolved_config.fullgraph,
        dynamic=resolved_config.dynamic,
    )
    eager_model = prepared_models.eager_model
    compiled_source = prepared_models.compiled_source
    compiled_model = prepared_models.compiled_model
    compile_error = prepared_models.compile_error
    compile_setup_seconds = prepared_models.compile_setup_seconds
    trainable_parameter_count = prepared_models.trainable_parameter_count

    ordered_batches = tuple(by_shape[shape] for shape in REQUIRED_COMPILE_SHAPE_CASES)
    if compiled_model is None:
        if compile_error is None:
            raise AssertionError("torch.compile returned no model and no error")
        reason = _failure_reason(
            "torch.compile setup failed",
            compile_error,
        )
        results = tuple(
            _failed_case(
                batch,
                trainable_parameter_count=trainable_parameter_count,
                reason=reason,
            )
            for batch in ordered_batches
        )
    else:
        results = tuple(
            _run_case(
                eager_model,
                compiled_model,
                compiled_source,
                batch,
                device=device,
                config=resolved_config,
                trainable_parameter_count=trainable_parameter_count,
            )
            for batch in ordered_batches
        )

    graph_break_count = max(
        0,
        torch_compile_graph_break_count() - prepared_models.graph_break_baseline,
    )
    runtime = CompileParityRuntime(
        backend=resolved_config.backend,
        mode=resolved_config.mode,
        device=str(device),
        dtype=resolved_config.dtype,
        torch_version=str(torch.__version__),
        tolerance_profile=COMPILE_PARITY_TOLERANCE_PROFILE,
        rtol=resolved_config.rtol,
        atol=resolved_config.atol,
        fullgraph=resolved_config.fullgraph,
        dynamic=resolved_config.dynamic,
        compile_setup_seconds=compile_setup_seconds,
        graph_break_count=graph_break_count,
    )
    report = CompileParityReport(
        kind=COMPILE_PARITY_KIND,
        schema_version=COMPILE_PARITY_SCHEMA_VERSION,
        created_at_utc=datetime.now(UTC).isoformat(),
        success=graph_break_count == 0
        and all(
            result.compile_succeeded and result.parity_passed for result in results
        ),
        provenance=provenance,
        runtime=runtime,
        cases=results,
        content_sha256="",
    )
    return _finalize_report(report)


def _expect_keys(
    value: object, expected: set[str], *, context: str
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{context} must be a JSON object")
    if any(type(key) is not str for key in value):
        raise ValueError(f"{context} keys must be strings")
    keys = set(value)
    if keys != expected:
        raise ValueError(
            f"{context} keys are invalid; missing={sorted(expected - keys)}, "
            f"unknown={sorted(keys - expected)}"
        )
    return cast(Mapping[str, object], value)


def _string(value: object, *, context: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{context} must be a non-empty string")
    return value


def _boolean(value: object, *, context: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{context} must be a boolean")
    return value


def _integer(value: object, *, context: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{context} must be an integer >= {minimum}")
    return value


def _number(value: object, *, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{context} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{context} must be finite and non-negative")
    return result


def _optional_number(value: object, *, context: str) -> float | None:
    if value is None:
        return None
    return _number(value, context=context)


def _strings(value: object, *, context: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{context} must be a string array")
    return tuple(
        _string(item, context=f"{context}[{index}]") for index, item in enumerate(value)
    )


def _provenance_from_dict(value: object) -> CompileParityProvenance:
    raw = _expect_keys(
        value,
        {
            "weights_sha256",
            "training_protocol_fingerprint",
            "dataset_fingerprint",
            "split_fingerprint",
        },
        context="compile parity provenance",
    )
    return CompileParityProvenance(
        weights_sha256=_string(raw["weights_sha256"], context="weights_sha256"),
        training_protocol_fingerprint=_string(
            raw["training_protocol_fingerprint"],
            context="training_protocol_fingerprint",
        ),
        dataset_fingerprint=_string(
            raw["dataset_fingerprint"], context="dataset_fingerprint"
        ),
        split_fingerprint=_string(
            raw["split_fingerprint"], context="split_fingerprint"
        ),
    )


def _runtime_from_dict(value: object) -> CompileParityRuntime:
    raw = _expect_keys(
        value,
        {
            "backend",
            "mode",
            "device",
            "dtype",
            "torch_version",
            "tolerance_profile",
            "rtol",
            "atol",
            "fullgraph",
            "dynamic",
            "compile_setup_seconds",
            "graph_break_count",
        },
        context="compile parity runtime",
    )
    dynamic_raw = raw["dynamic"]
    if dynamic_raw is not None and type(dynamic_raw) is not bool:
        raise ValueError("compile parity runtime.dynamic must be boolean or null")
    return CompileParityRuntime(
        backend=_string(raw["backend"], context="runtime.backend"),
        mode=_string(raw["mode"], context="runtime.mode"),
        device=_string(raw["device"], context="runtime.device"),
        dtype=_string(raw["dtype"], context="runtime.dtype"),
        torch_version=_string(raw["torch_version"], context="runtime.torch_version"),
        tolerance_profile=_string(
            raw["tolerance_profile"], context="runtime.tolerance_profile"
        ),
        rtol=_number(raw["rtol"], context="runtime.rtol"),
        atol=_number(raw["atol"], context="runtime.atol"),
        fullgraph=_boolean(raw["fullgraph"], context="runtime.fullgraph"),
        dynamic=cast(bool | None, dynamic_raw),
        compile_setup_seconds=_number(
            raw["compile_setup_seconds"], context="runtime.compile_setup_seconds"
        ),
        graph_break_count=_integer(
            raw["graph_break_count"], context="runtime.graph_break_count"
        ),
    )


_CASE_KEYS = {
    "case_id",
    "shape_case",
    "input_shape",
    "sample_ids",
    "compile_succeeded",
    "parity_passed",
    "trainable_parameter_count",
    "checked_gradient_count",
    "missing_gradient_parameters",
    "nonfinite_gradient_parameters",
    "mismatched_gradient_parameters",
    "eager_step_seconds",
    "compile_and_first_step_seconds",
    "compiled_replay_step_seconds",
    "eager_samples_per_second",
    "compiled_samples_per_second",
    "mean_max_abs_error",
    "mean_max_relative_error",
    "log_variance_max_abs_error",
    "log_variance_max_relative_error",
    "loss_max_abs_error",
    "loss_max_relative_error",
    "gradient_max_abs_error",
    "gradient_max_relative_error",
    "failure_reason",
}


def _case_from_dict(value: object, *, index: int) -> CompileParityCaseResult:
    context = f"compile parity cases[{index}]"
    raw = _expect_keys(value, _CASE_KEYS, context=context)
    shape_case_raw = _string(raw["shape_case"], context=f"{context}.shape_case")
    if shape_case_raw not in REQUIRED_COMPILE_SHAPE_CASES:
        raise ValueError(f"{context}.shape_case is unsupported")
    input_shape_raw = raw["input_shape"]
    if not isinstance(input_shape_raw, list) or len(input_shape_raw) != 4:
        raise ValueError(f"{context}.input_shape must contain four integers")
    input_shape = cast(
        tuple[int, int, int, int],
        tuple(
            _integer(item, context=f"{context}.input_shape[{position}]", minimum=1)
            for position, item in enumerate(input_shape_raw)
        ),
    )
    failure_reason_raw = raw["failure_reason"]
    if failure_reason_raw is not None and type(failure_reason_raw) is not str:
        raise ValueError(f"{context}.failure_reason must be a string or null")
    return CompileParityCaseResult(
        case_id=_string(raw["case_id"], context=f"{context}.case_id"),
        shape_case=cast(CompileShapeCase, shape_case_raw),
        input_shape=input_shape,
        sample_ids=_strings(raw["sample_ids"], context=f"{context}.sample_ids"),
        compile_succeeded=_boolean(
            raw["compile_succeeded"], context=f"{context}.compile_succeeded"
        ),
        parity_passed=_boolean(
            raw["parity_passed"], context=f"{context}.parity_passed"
        ),
        trainable_parameter_count=_integer(
            raw["trainable_parameter_count"],
            context=f"{context}.trainable_parameter_count",
            minimum=1,
        ),
        checked_gradient_count=_integer(
            raw["checked_gradient_count"],
            context=f"{context}.checked_gradient_count",
        ),
        missing_gradient_parameters=_strings(
            raw["missing_gradient_parameters"],
            context=f"{context}.missing_gradient_parameters",
        ),
        nonfinite_gradient_parameters=_strings(
            raw["nonfinite_gradient_parameters"],
            context=f"{context}.nonfinite_gradient_parameters",
        ),
        mismatched_gradient_parameters=_strings(
            raw["mismatched_gradient_parameters"],
            context=f"{context}.mismatched_gradient_parameters",
        ),
        eager_step_seconds=_optional_number(
            raw["eager_step_seconds"], context=f"{context}.eager_step_seconds"
        ),
        compile_and_first_step_seconds=_optional_number(
            raw["compile_and_first_step_seconds"],
            context=f"{context}.compile_and_first_step_seconds",
        ),
        compiled_replay_step_seconds=_optional_number(
            raw["compiled_replay_step_seconds"],
            context=f"{context}.compiled_replay_step_seconds",
        ),
        eager_samples_per_second=_optional_number(
            raw["eager_samples_per_second"],
            context=f"{context}.eager_samples_per_second",
        ),
        compiled_samples_per_second=_optional_number(
            raw["compiled_samples_per_second"],
            context=f"{context}.compiled_samples_per_second",
        ),
        mean_max_abs_error=_optional_number(
            raw["mean_max_abs_error"], context=f"{context}.mean_max_abs_error"
        ),
        mean_max_relative_error=_optional_number(
            raw["mean_max_relative_error"],
            context=f"{context}.mean_max_relative_error",
        ),
        log_variance_max_abs_error=_optional_number(
            raw["log_variance_max_abs_error"],
            context=f"{context}.log_variance_max_abs_error",
        ),
        log_variance_max_relative_error=_optional_number(
            raw["log_variance_max_relative_error"],
            context=f"{context}.log_variance_max_relative_error",
        ),
        loss_max_abs_error=_optional_number(
            raw["loss_max_abs_error"], context=f"{context}.loss_max_abs_error"
        ),
        loss_max_relative_error=_optional_number(
            raw["loss_max_relative_error"],
            context=f"{context}.loss_max_relative_error",
        ),
        gradient_max_abs_error=_optional_number(
            raw["gradient_max_abs_error"],
            context=f"{context}.gradient_max_abs_error",
        ),
        gradient_max_relative_error=_optional_number(
            raw["gradient_max_relative_error"],
            context=f"{context}.gradient_max_relative_error",
        ),
        failure_reason=cast(str | None, failure_reason_raw),
    )


def compile_parity_report_from_dict(value: object) -> CompileParityReport:
    """Decode and strictly validate a schema-v1 report mapping."""

    raw = _expect_keys(
        value,
        {
            "kind",
            "schema_version",
            "created_at_utc",
            "success",
            "provenance",
            "runtime",
            "cases",
            "content_sha256",
        },
        context="compile parity report",
    )
    cases_raw = raw["cases"]
    if not isinstance(cases_raw, list):
        raise ValueError("compile parity report.cases must be an array")
    report = CompileParityReport(
        kind=_string(raw["kind"], context="report.kind"),
        schema_version=_integer(
            raw["schema_version"], context="report.schema_version", minimum=1
        ),
        created_at_utc=_string(raw["created_at_utc"], context="report.created_at_utc"),
        success=_boolean(raw["success"], context="report.success"),
        provenance=_provenance_from_dict(raw["provenance"]),
        runtime=_runtime_from_dict(raw["runtime"]),
        cases=tuple(
            _case_from_dict(item, index=index) for index, item in enumerate(cases_raw)
        ),
        content_sha256=_string(raw["content_sha256"], context="report.content_sha256"),
    )
    validate_compile_parity_report(report)
    return report


def load_compile_parity_report(path: Path) -> CompileParityReport:
    """Load a report while rejecting malformed, unknown, or tampered
    content."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"compile parity report cannot be read: {path}: {error}"
        ) from error
    return compile_parity_report_from_dict(value)


def write_compile_parity_report(report: CompileParityReport, path: Path) -> Path:
    """Atomically publish a new report without replacing an existing
    artifact."""

    validate_compile_parity_report(report)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".new.tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                report.to_dict(),
                stream,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if load_compile_parity_report(temporary) != report:
            raise ValueError("compile parity report changed during JSON readback")
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise FileExistsError(
                f"compile parity report already exists and will not be replaced: {path}"
            ) from error
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    return path


__all__ = [
    "COMPILE_PARITY_KIND",
    "COMPILE_PARITY_SCHEMA_VERSION",
    "COMPILE_PARITY_TOLERANCE_PROFILE",
    "REQUIRED_COMPILE_SHAPE_CASES",
    "CompileParityBatch",
    "CompileParityCaseResult",
    "CompileParityConfig",
    "CompileParityProvenance",
    "CompileParityReport",
    "CompileParityRuntime",
    "CompileShapeCase",
    "compile_parity_report_from_dict",
    "compile_parity_report_sha256",
    "load_compile_parity_report",
    "run_compile_parity",
    "validate_compile_parity_report",
    "write_compile_parity_report",
]
