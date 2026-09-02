"""ONNX Runtime FP32 graph optimization and static INT8 QDQ mechanics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast, override

import numpy as np


def optimize_fp32(source: Path, output: Path) -> None:
    """Materialize ONNX Runtime's basic graph optimizations as a new
    artifact."""

    import onnxruntime as ort

    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
    options.optimized_model_filepath = str(output)
    ort.InferenceSession(
        str(source),
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )
    if not Path(output).is_file():
        raise RuntimeError("ONNX Runtime did not create the optimized FP32 artifact")


def quantize_static_qdq(
    source: Path,
    output: Path,
    feeds: Sequence[Mapping[str, np.ndarray[Any, Any]]],
) -> None:
    """Create a symmetric, per-channel static INT8 QDQ graph from input
    feeds."""

    if not feeds:
        raise ValueError("static QDQ calibration requires at least one input feed")
    from onnxruntime.quantization import (
        CalibrationDataReader,
        CalibrationMethod,
        QuantFormat,
        QuantType,
        quantize_static,
    )

    class _Reader(CalibrationDataReader):
        def __init__(self) -> None:
            self._iterator = iter(dict(feed) for feed in feeds)

        @override
        def get_next(self) -> dict[Any, Any]:
            return cast(dict[Any, Any], next(self._iterator, None))

        def rewind(self) -> None:
            self._iterator = iter(dict(feed) for feed in feeds)

    quantize_static(
        str(source),
        str(output),
        _Reader(),
        quant_format=QuantFormat.QDQ,
        activation_type=QuantType.QInt8,
        weight_type=QuantType.QInt8,
        per_channel=True,
        calibrate_method=CalibrationMethod.MinMax,
        op_types_to_quantize=("Conv", "MatMul", "Gemm"),
        extra_options={"ActivationSymmetric": True, "WeightSymmetric": True},
    )
