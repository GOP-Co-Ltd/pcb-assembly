"""静的 INT8 量子化の公開契約.

計画 §4.8 / §6.5 に対応する。実 ``onnxruntime.quantization`` を使い、実 ONNX を量子化する。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import cast

import attrs
import numpy as np
import onnxruntime
import pytest
from numpy.typing import NDArray

from ml.export.quantization import (
    DEFAULT_QUANTIZED_OPERATOR_TYPES,
    CalibrationSample,
    StaticQuantizationOptions,
    StaticQuantizationResult,
)
from tests.ml.export import support

OPTIONS = StaticQuantizationOptions()

# FP32 と INT8 の食い違いに許す上限。tiny model の実測は log 分散で 1.7e-2 だった
MAXIMUM_RELATIVE_DIFFERENCE = 0.05


def _samples(count: int = 8) -> tuple[CalibrationSample, ...]:
    # 整列されることを見たいので、わざと降順の ID を渡す
    return tuple(
        CalibrationSample(sample_id=f"train-{count - index:03d}", values=values)
        for index, values in enumerate(support.calibration_values(count))
    )


def _quantize(
    model_path: Path,
    *,
    source: Path | None = None,
    samples: Sequence[CalibrationSample] | None = None,
    options: StaticQuantizationOptions = OPTIONS,
) -> StaticQuantizationResult:
    result, error = StaticQuantizationResult.quantize(
        support.shared_fp32_model_path() if source is None else source,
        model_path,
        _samples() if samples is None else samples,
        options=options,
    )

    assert result is not None, error
    return result


class TestQuantize:
    """GroupNorm を含む実 model を静的量子化する."""

    def test_quantizes_a_model_whose_initializers_are_shared(self, tmp_path: Path):
        # 既定の op_types_to_quantize（全 op）は共有 initializer で失敗する構成
        result = _quantize(tmp_path / "int8.onnx")

        assert result.model_path.is_file()
        assert result.summary.node_domains == ("",)
        # summary は量子化後の graph のもの。QDQ の node は FP32 側には無い
        assert {"QuantizeLinear", "DequantizeLinear"} <= set(
            result.summary.operator_types
        )

    def test_keeps_the_dynamic_axes_of_the_source_model(self, tmp_path: Path):
        result = _quantize(tmp_path / "int8.onnx")

        assert (
            result.summary.verify_dynamic_dimensions(
                expected={
                    support.IMAGES_INPUT: {
                        2: support.HEIGHT_SYMBOL,
                        3: support.WIDTH_SYMBOL,
                    }
                }
            )
            is None
        )

    def test_records_how_the_model_was_quantized(self, tmp_path: Path):
        result = _quantize(tmp_path / "int8.onnx")

        assert result.record.quantized_operator_types == (
            DEFAULT_QUANTIZED_OPERATOR_TYPES
        )
        assert result.record.per_channel is False
        assert result.record.calibration_split == "train"
        assert result.record.calibration_sample_ids == tuple(
            sorted(sample.sample_id for sample in _samples())
        )
        assert result.record.validate() is None

    def test_stays_close_to_the_float32_model(self, tmp_path: Path):
        result = _quantize(tmp_path / "int8.onnx")
        values = support.input_values(seed=42)
        float32_session = onnxruntime.InferenceSession(
            str(support.shared_fp32_model_path()), providers=["CPUExecutionProvider"]
        )
        int8_session = onnxruntime.InferenceSession(
            str(result.model_path), providers=["CPUExecutionProvider"]
        )

        expected = float32_session.run(None, values)
        actual = int8_session.run(None, values)

        differences = [
            float(
                np.abs(np.asarray(left) - np.asarray(right)).max()
                / np.abs(np.asarray(left)).max()
            )
            for left, right in zip(expected, actual, strict=True)
        ]

        assert differences == pytest.approx([0.0, 0.0], abs=MAXIMUM_RELATIVE_DIFFERENCE)

    def test_reports_an_unrestricted_operator_type_list(self, tmp_path: Path):
        # 空にすると全 op が対象になり、QuantizationRecord.validate() を通らない
        # record を返す成果物になる。onnxruntime を呼ぶ前に入口で弾く
        result, error = StaticQuantizationResult.quantize(
            support.shared_fp32_model_path(),
            tmp_path / "int8.onnx",
            _samples(),
            options=attrs.evolve(OPTIONS, quantized_operator_types=()),
        )

        assert result is None
        assert error is not None
        assert "quantized_operator_types" in error
        assert list(tmp_path.iterdir()) == []

    def test_reports_too_few_calibration_samples(self, tmp_path: Path):
        result, error = StaticQuantizationResult.quantize(
            support.shared_fp32_model_path(),
            tmp_path / "int8.onnx",
            _samples(count=OPTIONS.minimum_calibration_samples - 1),
            options=OPTIONS,
        )

        assert result is None
        assert error is not None

    def test_reports_a_calibration_sample_with_an_unknown_input(self, tmp_path: Path):
        broken = tuple(
            attrs.evolve(
                sample,
                values={"unexpected_input": sample.values[support.CONDITIONING_INPUT]},
            )
            for sample in _samples()
        )

        result, error = StaticQuantizationResult.quantize(
            support.shared_fp32_model_path(),
            tmp_path / "int8.onnx",
            broken,
            options=OPTIONS,
        )

        assert result is None
        assert error is not None
        # 入力名の食い違いは onnxruntime も例外にするので、入口で弾いたことを見る
        assert "入力名が model と一致しません" in error

    def test_reports_a_duplicate_calibration_sample_id(self, tmp_path: Path):
        samples = _samples()
        duplicated = (samples[0], *samples[1:-1], samples[0])

        result, error = StaticQuantizationResult.quantize(
            support.shared_fp32_model_path(),
            tmp_path / "int8.onnx",
            duplicated,
            options=OPTIONS,
        )

        assert result is None
        assert error is not None
        assert "sample_id が重複しています" in error

    def test_reports_a_malformed_calibration_sample(self, tmp_path: Path):
        samples = _samples()
        broken = (attrs.evolve(samples[0], sample_id=""), *samples[1:])

        result, error = StaticQuantizationResult.quantize(
            support.shared_fp32_model_path(),
            tmp_path / "int8.onnx",
            broken,
            options=OPTIONS,
        )

        assert result is None
        assert error is not None
        assert "sample_id は空にできません" in error

    def test_reports_calibration_samples_of_the_wrong_element_type(
        self, tmp_path: Path
    ):
        # float64 のまま ORT へ渡すと 3rd-party の例外になる。理由文字列で返す
        broken = tuple(
            attrs.evolve(
                sample,
                values={
                    name: cast("NDArray[np.float32]", values.astype(np.float64))
                    for name, values in sample.values.items()
                },
            )
            for sample in _samples()
        )

        result, error = StaticQuantizationResult.quantize(
            support.shared_fp32_model_path(),
            tmp_path / "int8.onnx",
            broken,
            options=OPTIONS,
        )

        assert result is None
        assert error is not None

    def test_reports_a_source_model_that_does_not_exist(self, tmp_path: Path):
        result, error = StaticQuantizationResult.quantize(
            tmp_path / "absent.onnx",
            tmp_path / "int8.onnx",
            _samples(),
            options=OPTIONS,
        )

        assert result is None
        assert error is not None


class TestCalibrationSample:
    """Calibration に渡す 1 sample の検証."""

    def test_accepts_a_sample_taken_from_the_train_split(self):
        assert _samples()[0].validate() is None

    @pytest.mark.parametrize(
        "sample",
        [
            pytest.param(
                attrs.evolve(_samples()[0], sample_id=""), id="empty-sample-id"
            ),
            pytest.param(attrs.evolve(_samples()[0], values={}), id="no-values"),
            pytest.param(
                attrs.evolve(_samples()[0], values={"": np.zeros((1, 1), np.float32)}),
                id="empty-input-name",
            ),
            pytest.param(
                attrs.evolve(
                    _samples()[0],
                    values={
                        name: values[:0]
                        for name, values in _samples()[0].values.items()
                    },
                ),
                id="empty-array",
            ),
        ],
    )
    def test_reports_a_malformed_sample(self, sample: CalibrationSample):
        assert sample.validate() is not None


class TestOptions:
    """量子化設定の検証."""

    def test_accepts_the_shipping_defaults(self):
        assert OPTIONS.validate() is None
        assert OPTIONS.quantized_operator_types == DEFAULT_QUANTIZED_OPERATOR_TYPES

    @pytest.mark.parametrize(
        "options",
        [
            pytest.param(attrs.evolve(OPTIONS, calibration_split=""), id="empty-split"),
            pytest.param(
                attrs.evolve(OPTIONS, minimum_calibration_samples=0),
                id="no-minimum-samples",
            ),
            pytest.param(
                attrs.evolve(OPTIONS, quantized_operator_types=("Conv", "Conv")),
                id="duplicate-operator-type",
            ),
            pytest.param(
                attrs.evolve(OPTIONS, quantized_operator_types=("",)),
                id="empty-operator-type",
            ),
            pytest.param(
                attrs.evolve(OPTIONS, quantized_operator_types=()),
                id="no-operator-type",
            ),
        ],
    )
    def test_reports_malformed_options(self, options: StaticQuantizationOptions):
        assert options.validate() is not None
