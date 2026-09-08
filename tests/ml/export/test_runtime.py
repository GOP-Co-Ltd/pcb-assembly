"""推論 package の読み込みと実行の公開契約.

計画 §4.3 / §6.7 に対応する。

torch を使わない層だが、テスト側は eager と比べるために torch を使う。
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import numpy as np
import pytest
import torch
from numpy.typing import NDArray

from ml.artifact.package import CHECKSUM_FILENAME, ImmutablePackage
from ml.export.manifest import MANIFEST_FILENAME
from ml.export.runtime import OnnxInferenceModel
from tests.ml.export import support


def _load(package: Path) -> OnnxInferenceModel:
    model, error = OnnxInferenceModel.load(package)

    assert model is not None, error
    return model


def _published(tmp_path: Path) -> Path:
    return support.publish_model_package(tmp_path / "package").path


class TestLoad:
    """公開済み package を検証してから開く."""

    def test_exposes_the_manifest_and_the_payload_paths(self, tmp_path: Path):
        package = _published(tmp_path)

        model = _load(package)

        assert model.package_path == package
        assert model.model_path == package / support.MODEL_FILENAME
        assert model.manifest == support.build_manifest()

    def test_accepts_a_runtime_newer_than_the_minimum(self, tmp_path: Path):
        package = _published(tmp_path)

        assert OnnxInferenceModel.load(package, onnxruntime_version="99.0.0")[1] is None

    def test_reports_a_runtime_below_the_minimum(self, tmp_path: Path):
        package = _published(tmp_path)

        model, error = OnnxInferenceModel.load(package, onnxruntime_version="1.0.0")

        assert model is None
        assert error is not None

    def test_reports_a_tampered_payload(self, tmp_path: Path):
        package = _published(tmp_path)
        model_path = package / support.MODEL_FILENAME
        model_path.write_bytes(model_path.read_bytes() + b"\x00")

        model, error = OnnxInferenceModel.load(package)

        assert model is None
        assert error is not None
        assert CHECKSUM_FILENAME in error

    def test_reports_a_manifest_that_declares_a_payload_the_package_lacks(
        self, tmp_path: Path
    ):
        # manifest は preprocess.json を宣言するが、package には入れずに公開する
        manifest = support.build_manifest(extra_payload_filenames=("preprocess.json",))

        def write_payloads(directory: Path) -> None:
            (directory / support.MODEL_FILENAME).write_bytes(
                support.shared_fp32_model_path().read_bytes()
            )
            manifest.save(directory / MANIFEST_FILENAME)

        package = ImmutablePackage.publish(
            tmp_path / "package",
            payload_filenames=(MANIFEST_FILENAME, support.MODEL_FILENAME),
            write_payloads=write_payloads,
        )

        model, error = OnnxInferenceModel.load(package.path)

        assert model is None
        assert error is not None
        assert "preprocess.json" in error

    def test_reports_a_package_directory_that_does_not_exist(self, tmp_path: Path):
        model, error = OnnxInferenceModel.load(tmp_path / "absent")

        assert model is None
        assert error is not None

    def test_reports_a_package_without_a_manifest(self, tmp_path: Path):
        def write_payloads(directory: Path) -> None:
            (directory / support.MODEL_FILENAME).write_bytes(
                support.shared_fp32_model_path().read_bytes()
            )

        package = ImmutablePackage.publish(
            tmp_path / "package",
            payload_filenames=(support.MODEL_FILENAME,),
            write_payloads=write_payloads,
        )

        model, error = OnnxInferenceModel.load(package.path)

        assert model is None
        assert error is not None


class TestPredict:
    """開いた package で 1 件推論する."""

    def test_returns_the_same_values_as_the_eager_model(self, tmp_path: Path):
        model = _load(_published(tmp_path))
        images, conditioning = support.example_inputs(seed=5)
        with torch.no_grad():
            expected_mean, expected_log_variance = support.build_tiny_model()(
                images, conditioning
            )

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
        assert outputs[support.LOG_VARIANCE_OUTPUT] == pytest.approx(
            expected_log_variance.numpy(), abs=1e-5
        )

    def test_accepts_another_resolution(self, tmp_path: Path):
        model = _load(_published(tmp_path))

        outputs, error = model.predict(
            support.input_values(height=64, width=48, seed=6)
        )

        assert error is None
        assert outputs is not None
        assert outputs[support.MEAN_OUTPUT].shape == (1, 1)

    def test_reports_a_missing_input(self, tmp_path: Path):
        model = _load(_published(tmp_path))
        values = support.input_values()
        del values[support.CONDITIONING_INPUT]

        outputs, error = model.predict(values)

        assert outputs is None
        assert error is not None

    def test_reports_an_input_of_the_wrong_element_type(self, tmp_path: Path):
        model = _load(_published(tmp_path))
        values = support.input_values()
        values[support.IMAGES_INPUT] = cast(
            "NDArray[np.float32]", values[support.IMAGES_INPUT].astype(np.float64)
        )

        outputs, error = model.predict(values)

        assert outputs is None
        assert error is not None

    def test_reports_an_input_whose_fixed_axis_does_not_match(self, tmp_path: Path):
        model = _load(_published(tmp_path))

        outputs, error = model.predict(support.input_values(batch=3))

        assert outputs is None
        assert error is not None
