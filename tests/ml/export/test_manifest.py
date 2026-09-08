"""推論成果物 manifest の公開契約.

計画 §4.1 / §6.1 に対応する。
"""

from __future__ import annotations

import json
from pathlib import Path

import attrs
import pytest

from ml.export.manifest import (
    MANIFEST_FILENAME,
    InferenceManifest,
    QuantizationRecord,
    TensorContract,
)
from tests.ml.export import support

QUANTIZATION = QuantizationRecord(
    method="static-qdq",
    activation_type="QUInt8",
    weight_type="QInt8",
    per_channel=False,
    quantized_operator_types=("Conv", "Gemm"),
    calibration_split="train",
    calibration_sample_ids=("sample-001", "sample-002"),
)


def _first_input() -> TensorContract:
    return support.build_manifest().inputs[0]


def _first_output() -> TensorContract:
    return support.build_manifest().outputs[0]


class TestSaveAndLoad:
    """Manifest を JSON へ往復させる."""

    def test_round_trips_every_field(self, tmp_path: Path):
        manifest = support.build_manifest()

        manifest.save(tmp_path / MANIFEST_FILENAME)
        loaded, error = InferenceManifest.load(tmp_path / MANIFEST_FILENAME)

        assert error is None
        assert loaded == manifest

    def test_round_trips_a_quantization_record(self, tmp_path: Path):
        manifest = support.build_manifest(
            precision="static-int8", quantization=QUANTIZATION
        )

        manifest.save(tmp_path / MANIFEST_FILENAME)
        loaded, error = InferenceManifest.load(tmp_path / MANIFEST_FILENAME)

        assert error is None
        assert loaded is not None
        assert loaded.quantization == QUANTIZATION

    def test_reports_a_document_of_another_kind(self, tmp_path: Path):
        path = tmp_path / MANIFEST_FILENAME
        support.build_manifest().save(path)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["kind"] = "ml-something-else"
        path.write_text(json.dumps(document), encoding="utf-8")

        loaded, error = InferenceManifest.load(path)

        assert loaded is None
        assert error is not None
        assert "kind" in error

    def test_reports_an_unsupported_schema_version(self, tmp_path: Path):
        path = tmp_path / MANIFEST_FILENAME
        support.build_manifest().save(path)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["schema_version"] = document["schema_version"] + 1
        path.write_text(json.dumps(document), encoding="utf-8")

        loaded, error = InferenceManifest.load(path)

        assert loaded is None
        assert error is not None
        assert "schema_version" in error

    def test_reports_a_document_whose_contents_are_inconsistent(self, tmp_path: Path):
        # 構造は読めるが manifest として成立しない。load は validate() まで通す
        path = tmp_path / MANIFEST_FILENAME
        support.build_manifest().save(path)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["opset_version"] = 0
        path.write_text(json.dumps(document), encoding="utf-8")

        loaded, error = InferenceManifest.load(path)

        assert loaded is None
        assert error is not None
        assert "manifest の内容が不正です" in error

    def test_reports_a_missing_file(self, tmp_path: Path):
        loaded, error = InferenceManifest.load(tmp_path / "absent.json")

        assert loaded is None
        assert error is not None


class TestPayloadFilenames:
    """Package へ渡す payload 名の並び."""

    def test_includes_the_manifest_and_the_model(self):
        manifest = support.build_manifest()

        assert manifest.payload_filenames() == (
            MANIFEST_FILENAME,
            manifest.model_filename,
        )

    def test_sorts_every_payload_name(self):
        manifest = support.build_manifest(
            extra_payload_filenames=("preprocess.json", "evaluation.json")
        )

        assert manifest.validate() is None
        assert manifest.payload_filenames() == (
            "evaluation.json",
            MANIFEST_FILENAME,
            "model.onnx",
            "preprocess.json",
        )


class TestVerifyRuntime:
    """実行環境の onnxruntime 版が下限を満たすか."""

    @pytest.mark.parametrize(
        ("minimum", "installed"),
        [
            ("1.20", "1.20"),
            ("1.20", "1.20.0"),
            ("1.20.0", "1.20"),
            ("1.20", "1.29.0"),
            # 文字列比較なら "1.9" > "1.10" になる並び
            ("1.9", "1.10.0"),
            ("1.20.1", "1.20.10"),
        ],
    )
    def test_accepts_a_runtime_that_meets_the_minimum(
        self, minimum: str, installed: str
    ):
        manifest = support.build_manifest(minimum_onnxruntime_version=minimum)

        assert manifest.verify_runtime(onnxruntime_version=installed) is None

    @pytest.mark.parametrize(
        ("minimum", "installed"),
        [
            ("1.29", "1.20.0"),
            ("1.20.1", "1.20.0"),
            ("1.10", "1.9.0"),
            ("2.0", "1.29.0"),
        ],
    )
    def test_reports_a_runtime_below_the_minimum(self, minimum: str, installed: str):
        manifest = support.build_manifest(minimum_onnxruntime_version=minimum)

        error = manifest.verify_runtime(onnxruntime_version=installed)

        assert error is not None
        assert installed in error

    def test_reports_a_minimum_version_it_cannot_read(self):
        manifest = support.build_manifest(minimum_onnxruntime_version="unreleased")

        error = manifest.verify_runtime(onnxruntime_version="1.29.0")

        assert error is not None
        assert "minimum_onnxruntime_version を解釈できません" in error

    def test_reports_an_installed_version_it_cannot_read(self):
        error = support.build_manifest().verify_runtime(onnxruntime_version="unknown")

        assert error is not None
        assert "onnxruntime の版を解釈できません" in error


class TestTensorContract:
    """入出力 1 本の契約."""

    def test_lists_only_the_symbolic_dimensions(self):
        contract = attrs.evolve(
            _first_input(), dimensions=("1", "6", "height", "width")
        )

        assert contract.dynamic_dimension_names == ("height", "width")

    def test_accepts_a_contract_read_from_the_exported_graph(self):
        assert _first_input().validate() is None

    @pytest.mark.parametrize(
        "contract",
        [
            pytest.param(attrs.evolve(_first_input(), name=""), id="empty-name"),
            pytest.param(
                attrs.evolve(_first_input(), element_type=""), id="empty-element-type"
            ),
            pytest.param(
                attrs.evolve(_first_input(), dimensions=()), id="no-dimensions"
            ),
            pytest.param(
                attrs.evolve(_first_input(), dimensions=("1", "6", "", "width")),
                id="empty-dimension",
            ),
            pytest.param(
                attrs.evolve(_first_input(), dimensions=("1", "6", "he-ight", "width")),
                id="axis-is-neither-a-number-nor-an-identifier",
            ),
        ],
    )
    def test_reports_a_malformed_contract(self, contract: TensorContract):
        assert contract.validate() is not None

    def test_names_an_empty_axis_as_empty(self):
        # 空文字は「10 進数でも識別子でもない」検査にも掛かるため、理由まで見ないと
        # 空軸そのものの検査が消えても気付けない。
        contract = attrs.evolve(_first_input(), dimensions=("1", "6", "", "width"))

        error = contract.validate()

        assert error is not None
        assert "空の軸" in error


class TestQuantizationRecord:
    """量子化設定と校正 sample の記録.

    ``calibration_sample_ids`` は「どの sample で校正したか」の監査証跡そのもので、
    ``PromotionDecision`` は ``calibration_split`` しか見ないため、ここが唯一の防壁。
    """

    def test_accepts_the_shipping_record(self):
        assert QUANTIZATION.validate() is None

    @pytest.mark.parametrize(
        "record",
        [
            pytest.param(attrs.evolve(QUANTIZATION, method=""), id="empty-method"),
            pytest.param(
                attrs.evolve(QUANTIZATION, activation_type=""),
                id="empty-activation-type",
            ),
            pytest.param(
                attrs.evolve(QUANTIZATION, weight_type=""), id="empty-weight-type"
            ),
            pytest.param(
                attrs.evolve(QUANTIZATION, calibration_split=""),
                id="empty-calibration-split",
            ),
            pytest.param(
                attrs.evolve(QUANTIZATION, quantized_operator_types=()),
                id="no-operator-type",
            ),
            pytest.param(
                attrs.evolve(QUANTIZATION, quantized_operator_types=("Conv", "")),
                id="empty-operator-type",
            ),
            pytest.param(
                attrs.evolve(QUANTIZATION, quantized_operator_types=("Conv", "Conv")),
                id="duplicate-operator-type",
            ),
            pytest.param(
                attrs.evolve(QUANTIZATION, calibration_sample_ids=()),
                id="no-calibration-sample",
            ),
            pytest.param(
                attrs.evolve(QUANTIZATION, calibration_sample_ids=("sample-001", "")),
                id="empty-calibration-sample-id",
            ),
            pytest.param(
                attrs.evolve(
                    QUANTIZATION, calibration_sample_ids=("sample-001", "sample-001")
                ),
                id="duplicate-calibration-sample-id",
            ),
        ],
    )
    def test_reports_a_malformed_record(self, record: QuantizationRecord):
        assert record.validate() is not None

    def test_reports_a_malformed_record_through_the_manifest(self):
        # manifest 側は quantization の理由を前置き付きで伝える
        manifest = support.build_manifest(
            precision="static-int8",
            quantization=attrs.evolve(QUANTIZATION, calibration_sample_ids=()),
        )

        error = manifest.validate()

        assert error is not None
        assert error.startswith("quantization: ")


class TestValidate:
    """Manifest 全体の整合."""

    def test_accepts_the_exported_manifest(self):
        assert support.build_manifest().validate() is None

    def test_accepts_a_quantized_manifest(self):
        manifest = support.build_manifest(
            precision="static-int8", quantization=QUANTIZATION
        )

        assert manifest.validate() is None

    @pytest.mark.parametrize(
        "manifest",
        [
            pytest.param(
                support.build_manifest(model_filename=""), id="empty-model-filename"
            ),
            pytest.param(
                support.build_manifest(extra_payload_filenames=(MANIFEST_FILENAME,)),
                id="extra-repeats-the-manifest",
            ),
            pytest.param(
                support.build_manifest(extra_payload_filenames=("SHA256SUMS",)),
                id="extra-repeats-the-checksum-file",
            ),
            pytest.param(
                support.build_manifest(extra_payload_filenames=("model.onnx",)),
                id="extra-repeats-the-model",
            ),
            pytest.param(
                support.build_manifest(
                    extra_payload_filenames=("preprocess.json", "preprocess.json")
                ),
                id="duplicate-extra-payload",
            ),
            pytest.param(
                support.build_manifest(minimum_onnxruntime_version="1.29.0rc1"),
                id="non-numeric-runtime-version",
            ),
            pytest.param(
                support.build_manifest(minimum_onnxruntime_version=""),
                id="empty-runtime-version",
            ),
            pytest.param(support.build_manifest(inputs=()), id="no-inputs"),
            pytest.param(support.build_manifest(outputs=()), id="no-outputs"),
            pytest.param(
                support.build_manifest(
                    inputs=(attrs.evolve(_first_input(), dimensions=("1", "")),)
                ),
                id="malformed-input-contract",
            ),
            pytest.param(
                support.build_manifest(opset_version=0), id="non-positive-opset"
            ),
            pytest.param(
                support.build_manifest(precision="static-int8"),
                id="static-int8-without-a-quantization-record",
            ),
            pytest.param(
                support.build_manifest(quantization=QUANTIZATION),
                id="float32-with-a-quantization-record",
            ),
            # precision は Literal だが、JSON から読んだ値は実行時に何でも来る
            pytest.param(
                support.build_manifest(precision="float64"), id="unknown-precision"
            ),
            pytest.param(
                support.build_manifest(model_filename="weights/model.onnx"),
                id="model-filename-with-a-directory",
            ),
            pytest.param(
                support.build_manifest(onnx_ir_version=0), id="non-positive-ir-version"
            ),
            pytest.param(
                support.build_manifest(exporter_versions={}), id="no-exporter-version"
            ),
            pytest.param(
                support.build_manifest(exporter_versions={"": "2.12.1"}),
                id="empty-exporter-name",
            ),
            pytest.param(
                support.build_manifest(exporter_versions={"torch": ""}),
                id="empty-exporter-version",
            ),
            pytest.param(
                support.build_manifest(training_run_id=""), id="empty-training-run-id"
            ),
            pytest.param(
                support.build_manifest(dataset_fingerprint=""),
                id="empty-dataset-fingerprint",
            ),
            pytest.param(
                support.build_manifest(split_fingerprint=""),
                id="empty-split-fingerprint",
            ),
            pytest.param(
                support.build_manifest(inputs=(_first_input(), _first_input())),
                id="duplicate-input-name",
            ),
            pytest.param(
                support.build_manifest(
                    outputs=(_first_output(), _first_output()),
                ),
                id="duplicate-output-name",
            ),
        ],
    )
    def test_reports_a_malformed_manifest(self, manifest: InferenceManifest):
        assert manifest.validate() is not None
