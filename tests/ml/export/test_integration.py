"""Export から package 読み込みまでの通し.

計画 §6.9 に対応する。eager model → FP32 export → parity → INT8 量子化 → manifest →
package 公開 → 読み込み → 推論 → promote 判定を 1 本につなぐ。
"""

from __future__ import annotations

from pathlib import Path

import attrs
import numpy as np
import pytest
import torch

from ml.artifact.package import CHECKSUM_FILENAME
from ml.evaluation.compile_parity import ParityTolerance
from ml.export.manifest import MANIFEST_FILENAME, InferenceManifest, Precision
from ml.export.onnx_export import (
    DynamicDimension,
    OnnxExportOptions,
    OnnxExportResult,
)
from ml.export.parity import OnnxParityResult, ParityCase
from ml.export.promotion import (
    AccuracyEvidence,
    AccuracyGate,
    LatencyEvidence,
    LatencyGate,
    PromotionCandidate,
    PromotionDecision,
)
from ml.export.quantization import (
    CalibrationSample,
    StaticQuantizationOptions,
    StaticQuantizationResult,
)
from ml.export.runtime import OnnxInferenceModel
from tests.ml.export import support

TOLERANCE = ParityTolerance(relative=1e-4, absolute=1e-6)
EVALUATED_SPLIT = "validation"
FLOAT32 = "float32-candidate"
INT8 = "static-int8-candidate"

OPTIONS = OnnxExportOptions(
    input_names=support.INPUT_NAMES,
    output_names=support.OUTPUT_NAMES,
    dynamic_dimensions=(
        DynamicDimension(
            input_name=support.IMAGES_INPUT, axis=2, symbol=support.HEIGHT_SYMBOL
        ),
        DynamicDimension(
            input_name=support.IMAGES_INPUT, axis=3, symbol=support.WIDTH_SYMBOL
        ),
    ),
)


@attrs.frozen(eq=False)
class _Pipeline:
    """通しで作った成果物一式."""

    float32_package: Path
    static_int8_package: Path
    float32_manifest: InferenceManifest
    static_int8_manifest: InferenceManifest
    parity: OnnxParityResult
    float32_bytes: int
    static_int8_bytes: int


def _package_bytes(package: Path) -> int:
    return sum(entry.stat().st_size for entry in package.iterdir())


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory: pytest.TempPathFactory) -> _Pipeline:
    """Export から package 公開までを 1 度だけ通す."""

    directory = tmp_path_factory.mktemp("export-integration")
    model = support.build_tiny_model()

    exported, error = OnnxExportResult.export(
        model,
        support.example_inputs(),
        directory / "float32.onnx",
        options=OPTIONS,
    )
    assert exported is not None, error

    parity, error = OnnxParityResult.measure(
        model,
        exported.model_path,
        (
            ParityCase(case_id="small", inputs=support.example_inputs(height=16)),
            ParityCase(case_id="tall", inputs=support.example_inputs(height=64)),
        ),
        input_names=support.INPUT_NAMES,
        output_names=support.OUTPUT_NAMES,
        tolerance=TOLERANCE,
        positive_output_names=(support.MEAN_OUTPUT,),
    )
    assert parity is not None, error

    quantized, error = StaticQuantizationResult.quantize(
        exported.model_path,
        directory / "static-int8.onnx",
        tuple(
            CalibrationSample(sample_id=f"train-{index:03d}", values=values)
            for index, values in enumerate(support.calibration_values(8))
        ),
        options=StaticQuantizationOptions(),
    )
    assert quantized is not None, error

    float32_manifest = support.build_manifest()
    static_int8_manifest = support.build_manifest(
        precision="static-int8", quantization=quantized.record
    )
    float32_package = support.publish_model_package(
        directory / "float32-package",
        manifest=float32_manifest,
        model_path=exported.model_path,
    )
    static_int8_package = support.publish_model_package(
        directory / "static-int8-package",
        manifest=static_int8_manifest,
        model_path=quantized.model_path,
    )
    return _Pipeline(
        float32_package=float32_package.path,
        static_int8_package=static_int8_package.path,
        float32_manifest=float32_manifest,
        static_int8_manifest=static_int8_manifest,
        parity=parity,
        float32_bytes=_package_bytes(float32_package.path),
        static_int8_bytes=_package_bytes(static_int8_package.path),
    )


def _candidate(
    candidate_id: str,
    pipeline: _Pipeline,
    *,
    precision: Precision,
    artifact_bytes: int,
    p95_seconds: float | None,
) -> PromotionCandidate:
    manifest = (
        pipeline.float32_manifest
        if precision == "float32"
        else pipeline.static_int8_manifest
    )
    return PromotionCandidate(
        candidate_id=candidate_id,
        precision=precision,
        artifact_bytes=artifact_bytes,
        export_parity_passed=pipeline.parity.passed,
        accuracy=AccuracyEvidence(
            primary_score=0.05,
            one_standard_deviation_coverage=0.683,
            evaluated_split=EVALUATED_SPLIT,
        ),
        latency=(
            None
            if p95_seconds is None
            else LatencyEvidence(
                device_label="raspberry-pi-5",
                worst_p95_seconds=p95_seconds,
                cold_start_seconds=p95_seconds * 4.0,
            )
        ),
        quantization=manifest.quantization,
    )


class TestExportedPackage:
    """通しで作った package が eager と同じ値を返す."""

    def test_the_float32_package_predicts_like_the_eager_model(
        self, pipeline: _Pipeline
    ):
        model, error = OnnxInferenceModel.load(pipeline.float32_package)
        assert model is not None, error
        images, conditioning = support.example_inputs(height=48, width=64, seed=11)
        with torch.no_grad():
            expected_mean, _ = support.build_tiny_model()(images, conditioning)

        outputs, error = model.predict(
            {
                support.IMAGES_INPUT: images.numpy(),
                support.CONDITIONING_INPUT: conditioning.numpy(),
            }
        )

        assert error is None
        assert outputs is not None
        assert outputs[support.MEAN_OUTPUT] == pytest.approx(
            expected_mean.numpy(), abs=1e-5
        )

    def test_the_package_holds_the_weights_in_a_single_payload(
        self, pipeline: _Pipeline
    ):
        # export が重みを model.onnx.data へ切り出すと、checksum は通るのに
        # load が外部データを見つけられずに落ちる
        assert sorted(entry.name for entry in pipeline.float32_package.iterdir()) == [
            CHECKSUM_FILENAME,
            MANIFEST_FILENAME,
            support.MODEL_FILENAME,
        ]

        model, error = OnnxInferenceModel.load(pipeline.float32_package)

        assert model is not None, error
        assert model.predict(support.input_values(seed=13))[1] is None

    def test_the_export_parity_passed(self, pipeline: _Pipeline):
        assert pipeline.parity.passed is True
        assert all(
            case.non_positive_output_names == () for case in pipeline.parity.cases
        )

    def test_the_static_int8_package_stays_close_to_the_eager_model(
        self, pipeline: _Pipeline
    ):
        model, error = OnnxInferenceModel.load(pipeline.static_int8_package)
        assert model is not None, error
        images, conditioning = support.example_inputs(seed=12)
        with torch.no_grad():
            expected_mean, _ = support.build_tiny_model()(images, conditioning)

        outputs, error = model.predict(
            {
                support.IMAGES_INPUT: images.numpy(),
                support.CONDITIONING_INPUT: conditioning.numpy(),
            }
        )

        assert error is None
        assert outputs is not None
        difference = (
            np.abs(outputs[support.MEAN_OUTPUT] - expected_mean.numpy()).max()
            / np.abs(expected_mean.numpy()).max()
        )
        assert float(difference) < 0.05

    def test_the_static_int8_manifest_records_the_calibration_split(
        self, pipeline: _Pipeline
    ):
        model, error = OnnxInferenceModel.load(pipeline.static_int8_package)

        assert model is not None, error
        assert model.manifest.quantization is not None
        assert model.manifest.quantization.calibration_split == "train"
        assert model.manifest.precision == "static-int8"


class TestPromotionOverTheRealArtifacts:
    """実成果物から作った候補を promote 判定へ通す."""

    def test_promotes_the_float32_candidate_when_the_int8_is_not_faster(
        self, pipeline: _Pipeline
    ):
        decision, error = PromotionDecision.decide(
            [
                _candidate(
                    FLOAT32,
                    pipeline,
                    precision="float32",
                    artifact_bytes=pipeline.float32_bytes,
                    p95_seconds=0.400,
                ),
                _candidate(
                    INT8,
                    pipeline,
                    precision="static-int8",
                    artifact_bytes=pipeline.static_int8_bytes,
                    p95_seconds=0.600,
                ),
            ],
            accuracy_gate=AccuracyGate(),
            latency_gate=LatencyGate(),
        )

        assert error is None
        assert decision is not None
        assert decision.promoted_candidate_id == FLOAT32

    def test_rejects_the_int8_candidate_without_latency_evidence(
        self, pipeline: _Pipeline
    ):
        decision, error = PromotionDecision.decide(
            [
                _candidate(
                    FLOAT32,
                    pipeline,
                    precision="float32",
                    artifact_bytes=pipeline.float32_bytes,
                    p95_seconds=0.400,
                ),
                _candidate(
                    INT8,
                    pipeline,
                    precision="static-int8",
                    artifact_bytes=pipeline.static_int8_bytes,
                    p95_seconds=None,
                ),
            ],
            accuracy_gate=AccuracyGate(),
            latency_gate=LatencyGate(),
        )

        assert error is None
        assert decision is not None
        assert decision.promoted_candidate_id == FLOAT32
        assert tuple(rejected.candidate_id for rejected in decision.rejected) == (INT8,)
