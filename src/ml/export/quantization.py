"""ONNX model の static INT8 量子化.

Static 量子化は校正 sample の分布に依存するので、どの split の、どの sample を
使ったかを成果物へ残す。

既定で量子化するのは ``Conv`` と ``Gemm`` だけとする。

dynamo exporter は同じ値の初期化子を重複排除するため、全 operator を対象に
すると共有 initializer で失敗する。

失敗は例外にせず理由文字列で返す。

量子化できるかどうかは model 構成と onnxruntime の版に依存する条件のため。
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import override

import attrs
import numpy as np
from numpy.typing import NDArray
from onnxruntime.quantization import (
    CalibrationDataReader,
    QuantFormat,
    QuantType,
    quantize_static,
)
from onnxruntime.quantization.shape_inference import quant_pre_process

from ml.export.graph import OnnxGraphSummary
from ml.export.manifest import QuantizationRecord
from ml.export.promotion import CALIBRATION_SPLIT

DEFAULT_QUANTIZED_OPERATOR_TYPES = ("Conv", "Gemm")

_QUANTIZATION_METHOD = "onnxruntime-static-qdq"
_ACTIVATION_TYPE = "QUInt8"
_WEIGHT_TYPE = "QInt8"
_PREPROCESSED_FILENAME = "preprocessed.onnx"


@attrs.frozen(eq=False)
class CalibrationSample:
    """校正 1 回ぶんの入力.

    ndarray を持つので ``eq=False`` にする。

    attrs の既定 ``__eq__`` は配列比較が bool にならず壊れるため。
    """

    sample_id: str
    values: Mapping[str, NDArray[np.float32]]

    def validate(self) -> str | None:
        """Sample の id と入力の綴りを検証する."""

        if not self.sample_id:
            return "sample_id は空にできません"
        if not self.values:
            return f"values は 1 入力以上が必要です: {self.sample_id}"
        for name, array in self.values.items():
            if not name:
                return f"values に空の入力名があります: {self.sample_id}"
            if array.size == 0:
                return f"values が空の配列です: {self.sample_id} の {name}"
        return None


@attrs.frozen
class StaticQuantizationOptions:
    """Static 量子化の設定.

    ``quantized_operator_types`` は 1 個以上が必要。空にすると onnxruntime が
    全 operator を対象とするうえ、:class:`QuantizationRecord` が空の記録を
    拒否するので、成功しても検証を通らない成果物になる。
    """

    calibration_split: str = CALIBRATION_SPLIT
    per_channel: bool = False
    quantized_operator_types: tuple[str, ...] = DEFAULT_QUANTIZED_OPERATOR_TYPES
    minimum_calibration_samples: int = 8

    def validate(self) -> str | None:
        """量子化設定の綴りと件数の下限を検証する."""

        if not self.calibration_split:
            return "calibration_split は空にできません"
        if not self.quantized_operator_types:
            return (
                "quantized_operator_types は 1 個以上が必要です。"
                "空にすると QuantizationRecord が拒否する成果物になります"
            )
        if any(not item for item in self.quantized_operator_types):
            return "quantized_operator_types に空の operator 名があります"
        if len(set(self.quantized_operator_types)) != len(
            self.quantized_operator_types
        ):
            return (
                "quantized_operator_types が重複しています: "
                f"{sorted(self.quantized_operator_types)}"
            )
        if self.minimum_calibration_samples < 1:
            return (
                "minimum_calibration_samples は正の整数が必要です: "
                f"{self.minimum_calibration_samples}"
            )
        return None


@attrs.frozen
class StaticQuantizationResult:
    """量子化して検査まで通った INT8 model."""

    model_path: Path
    summary: OnnxGraphSummary
    record: QuantizationRecord

    @classmethod
    def quantize(
        cls,
        source_model_path: Path,
        model_path: Path,
        samples: Sequence[CalibrationSample],
        *,
        options: StaticQuantizationOptions,
    ) -> tuple[StaticQuantizationResult | None, str | None]:
        """FP32 model を校正 sample で static INT8 へ変換する.

        変換前に元 model を検査し、校正 sample の入力名と突き合わせる。

        onnxruntime が投げる例外はすべて理由文字列に変換する。
        """

        source = Path(source_model_path)
        destination = Path(model_path)
        if error := options.validate():
            return None, error
        source_summary, error = OnnxGraphSummary.inspect(source)
        if source_summary is None:
            return None, error
        if error := _validate_samples(samples, source_summary, options):
            return None, error

        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f".{destination.name}.", dir=destination.parent
        ) as workspace:
            preprocessed = Path(workspace) / _PREPROCESSED_FILENAME
            try:
                quant_pre_process(str(source), str(preprocessed))
                quantize_static(
                    str(preprocessed),
                    str(destination),
                    _SampleReader(samples),
                    quant_format=QuantFormat.QDQ,
                    activation_type=QuantType.QUInt8,
                    weight_type=QuantType.QInt8,
                    per_channel=options.per_channel,
                    op_types_to_quantize=list(options.quantized_operator_types),
                )
            except Exception as error:  # noqa: BLE001 - 量子化の失敗は理由にする
                destination.unlink(missing_ok=True)
                return None, f"static INT8 量子化に失敗しました: {error}"

        summary, error = OnnxGraphSummary.inspect(destination)
        if summary is None:
            destination.unlink(missing_ok=True)
            return None, error
        return (
            cls(
                model_path=destination,
                summary=summary,
                record=QuantizationRecord(
                    method=_QUANTIZATION_METHOD,
                    activation_type=_ACTIVATION_TYPE,
                    weight_type=_WEIGHT_TYPE,
                    per_channel=options.per_channel,
                    quantized_operator_types=options.quantized_operator_types,
                    calibration_split=options.calibration_split,
                    calibration_sample_ids=tuple(
                        sorted(sample.sample_id for sample in samples)
                    ),
                ),
            ),
            None,
        )


class _SampleReader(CalibrationDataReader):
    """校正 sample を onnxruntime の calibration へ 1 件ずつ渡す.

    3rd-party の ABC を実装しているのは API 要件であって、我々の抽象ではない。
    """

    def __init__(self, samples: Sequence[CalibrationSample]) -> None:
        self._remaining = iter([dict(sample.values) for sample in samples])

    # 基底の注釈は ``-> dict`` だが、``__next__`` は ``None`` を終端として
    # 扱う。実際の契約は「尽きたら None」なので、注釈側に合わせない。
    @override
    def get_next(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
    ) -> dict[str, NDArray[np.float32]] | None:
        """次の校正入力を返す.

        尽きたら ``None`` を返す。
        """

        return next(self._remaining, None)


def _validate_samples(
    samples: Sequence[CalibrationSample],
    source_summary: OnnxGraphSummary,
    options: StaticQuantizationOptions,
) -> str | None:
    if len(samples) < options.minimum_calibration_samples:
        return (
            "校正 sample が足りません: "
            f"{len(samples)}（最低 {options.minimum_calibration_samples} 件）"
        )
    identifiers = [sample.sample_id for sample in samples]
    if len(set(identifiers)) != len(identifiers):
        return f"sample_id が重複しています: {sorted(identifiers)}"
    expected = {tensor.name for tensor in source_summary.inputs}
    for sample in samples:
        if error := sample.validate():
            return error
        if set(sample.values) != expected:
            return (
                f"校正 sample の入力名が model と一致しません: "
                f"{sample.sample_id} は {sorted(sample.values)}"
                f"（model は {sorted(expected)}）"
            )
    return None


__all__ = [
    "DEFAULT_QUANTIZED_OPERATOR_TYPES",
    "CalibrationSample",
    "StaticQuantizationOptions",
    "StaticQuantizationResult",
]
