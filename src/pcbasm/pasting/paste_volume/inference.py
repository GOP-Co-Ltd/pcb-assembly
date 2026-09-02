"""Promoted ONNX model packageの検証と公開推論API."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, TypeGuard, cast

import attrs
import numpy as np

from pcbasm.vision import ImageArray

from .formal_artifact import FormalArtifactAttestation

MODEL_PACKAGE_SCHEMA_VERSION = 2
EVALUATION_SCHEMA_VERSION = 2
ACTIVE_MODEL_POINTER_SCHEMA_VERSION = 1
_PACKAGE_FILES = frozenset(
    {"manifest.json", "model.onnx", "preprocess.json", "evaluation.json"}
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PREFIXED_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMPILE_SHAPES = {
    "minimum": (32, 32),
    "maximum-area": (512, 512),
    "portrait": (1024, 256),
    "landscape": (256, 1024),
}
_COMPILE_CASES = frozenset((*_COMPILE_SHAPES, "training-batch"))
_BENCHMARK_CATEGORIES = ("small", "medium", "large", "portrait", "landscape")


class PasteVolumeModelPackageError(RuntimeError):
    """Model packageがproduction契約を満たさない."""


class PasteVolumeRuntimeError(RuntimeError):
    """推論runtimeを初期化できない."""


@attrs.frozen
class PasteVolumePrediction:
    """1組の画像から推定した体積と採用可否."""

    mean_volume_ul: float
    std_volume_ul: float
    relative_std: float
    accepted: bool
    rejection_reason: str | None
    model_id: str

    def to_dict(self) -> dict[str, object]:
        """CLI/API境界で非有限の未取得値をJSON nullとして表現する."""

        return {
            "mean_volume_ul": _finite_or_none(self.mean_volume_ul),
            "std_volume_ul": _finite_or_none(self.std_volume_ul),
            "relative_std": _finite_or_none(self.relative_std),
            "accepted": self.accepted,
            "rejection_reason": self.rejection_reason,
            "model_id": self.model_id,
        }


@attrs.frozen
class PasteVolumeModelInfo:
    """Job logへ記録するimmutableなmodel identity."""

    model_id: str
    model_name: str
    model_version: str
    model_checksum: str
    package_path: Path
    format: str


class PasteVolumeEstimator(Protocol):
    """推論backend形式を隠す公開契約."""

    @property
    def model_info(self) -> PasteVolumeModelInfo: ...

    def predict(
        self,
        pre_rgb: ImageArray,
        post_rgb: ImageArray,
        *,
        pixel_per_mm: float,
    ) -> PasteVolumePrediction: ...


@attrs.frozen
class VerifiedModelPackage:
    """Checksumとschemaを検証済みのpackage metadata."""

    path: Path
    manifest: Mapping[str, Any]
    preprocess: Mapping[str, Any]
    evaluation: Mapping[str, Any]
    model_sha256: str


class _OnnxPasteVolumeEstimator:
    def __init__(self, package: VerifiedModelPackage) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - dependency error only
            raise PasteVolumeRuntimeError(
                "ONNX Runtimeが必要です。ml-runtime dependency groupをinstallしてください"
            ) from exc

        runtime = _required_mapping(package.manifest, "runtime")
        minimum_version = _required_string(runtime, "minimum_version")
        if _version_tuple(ort.__version__) < _version_tuple(minimum_version):
            raise PasteVolumeRuntimeError(
                f"onnxruntime>={minimum_version}が必要です: installed={ort.__version__}"
            )
        providers = set(ort.get_available_providers())
        if "CPUExecutionProvider" not in providers:
            raise PasteVolumeRuntimeError(
                "ONNX Runtime CPUExecutionProviderがありません"
            )
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        try:
            self._session = ort.InferenceSession(
                str(package.path / "model.onnx"),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
        except Exception as exc:
            raise PasteVolumeRuntimeError(
                f"ONNX Runtime sessionを初期化できません: {exc}"
            ) from exc

        self._package = package
        _validate_embedded_release_metadata(
            package.manifest,
            self._session.get_modelmeta().custom_metadata_map,
        )
        model = _required_mapping(package.manifest, "model")
        model_name = _required_string(model, "name")
        model_version = _required_string(model, "version")
        model_id = _required_string(model, "id")
        model_format = _required_string(model, "format")
        self._model_info = PasteVolumeModelInfo(
            model_id=model_id,
            model_name=model_name,
            model_version=model_version,
            model_checksum=package.model_sha256,
            package_path=package.path,
            format=model_format,
        )
        contract = _required_mapping(package.manifest, "input_contract")
        inputs = _required_mapping(contract, "inputs")
        outputs = _required_mapping(contract, "outputs")
        self._image_input = _required_string(inputs, "image_6ch")
        self._mask_input = _required_string(inputs, "valid_pixel_mask")
        self._scale_input = _required_string(inputs, "pixel_per_mm")
        self._mean_output = _required_string(outputs, "mean_volume_ul")
        self._log_variance_output = _required_string(outputs, "log_variance_volume_ul2")
        actual_inputs = {item.name: item.type for item in self._session.get_inputs()}
        actual_outputs = {item.name: item.type for item in self._session.get_outputs()}
        if actual_inputs != {
            self._image_input: "tensor(float)",
            self._mask_input: "tensor(bool)",
            self._scale_input: "tensor(float)",
        }:
            raise PasteVolumeRuntimeError(
                f"ONNX input contractがmanifestと一致しません: {actual_inputs}"
            )
        if actual_outputs != {
            self._mean_output: "tensor(float)",
            self._log_variance_output: "tensor(float)",
        }:
            raise PasteVolumeRuntimeError(
                f"ONNX output contractがmanifestと一致しません: {actual_outputs}"
            )
        threshold = package.manifest.get("uncertainty_relative_std_threshold")
        if not _is_positive_finite(threshold):
            raise PasteVolumeModelPackageError(
                "uncertainty_relative_std_thresholdは正の有限値が必要です"
            )
        self._relative_std_threshold = float(threshold)

    @property
    def model_info(self) -> PasteVolumeModelInfo:
        return self._model_info

    def predict(
        self,
        pre_rgb: ImageArray,
        post_rgb: ImageArray,
        *,
        pixel_per_mm: float,
    ) -> PasteVolumePrediction:
        _validate_rgb_pair(pre_rgb, post_rgb, pixel_per_mm=pixel_per_mm)
        try:
            image, mask, transformed_scale = _preprocess_inputs(
                pre_rgb,
                post_rgb,
                pixel_per_mm=float(pixel_per_mm),
                config=self._package.preprocess,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"画像の前処理に失敗しました: {exc}") from exc

        coverage_reason = _coverage_rejection_reason(
            self._package.manifest,
            original_height=int(pre_rgb.shape[0]),
            original_width=int(pre_rgb.shape[1]),
            pixel_per_mm=float(pixel_per_mm),
        )
        try:
            result = self._session.run(
                [self._mean_output, self._log_variance_output],
                {
                    self._image_input: image,
                    self._mask_input: mask,
                    self._scale_input: transformed_scale,
                },
            )
        except Exception as exc:
            return _rejected_prediction(
                self._model_info.model_id,
                f"runtime inferenceに失敗しました: {exc}",
            )
        if len(result) != 2:
            return _rejected_prediction(
                self._model_info.model_id, "model output数が2ではありません"
            )
        mean = _single_output(result[0])
        log_variance = _single_output(result[1])
        if mean is None or not math.isfinite(mean) or mean <= 0:
            return _rejected_prediction(
                self._model_info.model_id, "mean_volume_ulが正の有限値ではありません"
            )
        if log_variance is None or not math.isfinite(log_variance):
            return _rejected_prediction(
                self._model_info.model_id,
                "log_variance_volume_ul2が有限値ではありません",
                mean=mean,
            )
        try:
            std = math.exp(0.5 * log_variance)
        except OverflowError:
            std = math.inf
        if not math.isfinite(std) or std < 0:
            return _rejected_prediction(
                self._model_info.model_id,
                "std_volume_ulが非負の有限値ではありません",
                mean=mean,
            )
        relative_std = std / mean
        reason = coverage_reason
        if reason is None and relative_std > self._relative_std_threshold:
            reason = (
                f"relative_std={relative_std:.6g}がthreshold="
                f"{self._relative_std_threshold:.6g}を超えました"
            )
        return PasteVolumePrediction(
            mean_volume_ul=mean,
            std_volume_ul=std,
            relative_std=relative_std,
            accepted=reason is None,
            rejection_reason=reason,
            model_id=self._model_info.model_id,
        )


def load_paste_volume_estimator(model_package: Path) -> PasteVolumeEstimator:
    """Promotion gateを通過したpackageだけをproduction向けにloadする."""

    package = verify_model_package(model_package, require_promoted=True)
    return _OnnxPasteVolumeEstimator(package)


def load_candidate_paste_volume_estimator(model_package: Path) -> PasteVolumeEstimator:
    """候補評価専用に、厳密検証済みcandidate packageをloadする."""

    package = verify_model_package(model_package, require_promoted=False)
    return _OnnxPasteVolumeEstimator(package)


def load_active_paste_volume_estimator(pointer_file: Path) -> PasteVolumeEstimator:
    """Active pointerを解決してpromoted packageをloadする."""

    return load_paste_volume_estimator(read_paste_volume_active_model(pointer_file))


def load_configured_paste_volume_estimator(path: Path) -> PasteVolumeEstimator:
    """Machine設定のpackage directoryまたはactive pointerをloadする."""

    resolved = Path(path).expanduser()
    if resolved.is_dir():
        return load_paste_volume_estimator(resolved)
    if resolved.is_file():
        return load_active_paste_volume_estimator(resolved)
    raise PasteVolumeModelPackageError(f"model設定pathが存在しません: {resolved}")


def read_paste_volume_active_model(pointer_file: Path) -> Path:
    """Atomic active pointerの現在package pathを返す."""

    pointer_path = Path(pointer_file).expanduser().resolve()
    pointer = _read_json_object(pointer_path, description="active model pointer")
    if pointer.get("schema_version") != ACTIVE_MODEL_POINTER_SCHEMA_VERSION:
        raise PasteVolumeModelPackageError("未知のactive model pointer schemaです")
    active = _required_string(pointer, "active_model_path")
    active_path = Path(active)
    if not active_path.is_absolute():
        active_path = pointer_path.parent / active_path
    active_path = active_path.resolve()
    if pointer_path.is_relative_to(active_path):
        raise PasteVolumeModelPackageError(
            "active model pointerはimmutable model packageの外に配置してください"
        )
    if not active_path.is_dir():
        raise PasteVolumeModelPackageError(
            f"active model packageが存在しません: {active_path}"
        )
    stored_hash = _required_string(pointer, "active_model_sha256")
    stored_id = _required_string(pointer, "active_model_id")
    verified = verify_model_package(active_path, require_promoted=True)
    if verified.model_sha256 != stored_hash:
        raise PasteVolumeModelPackageError("active pointerのmodel checksumが不一致です")
    model = _required_mapping(verified.manifest, "model")
    if model.get("id") != stored_id:
        raise PasteVolumeModelPackageError("active pointerのmodel idが不一致です")
    return active_path


def verify_model_package(
    model_package: Path, *, require_promoted: bool
) -> VerifiedModelPackage:
    """package全fileのchecksum、schema、runtime、promotion証跡を検証する."""

    package_path = Path(model_package).expanduser().resolve()
    if not package_path.is_dir():
        raise PasteVolumeModelPackageError(
            f"model package directoryが存在しません: {package_path}"
        )
    actual_entries = {entry.name for entry in package_path.iterdir()}
    expected_entries = {*_PACKAGE_FILES, "SHA256SUMS"}
    if actual_entries != expected_entries:
        raise PasteVolumeModelPackageError(
            "model packageは規定の5 fileだけを含めてください"
        )
    for filename in expected_entries:
        path = package_path / filename
        if not path.is_file() or path.is_symlink():
            raise PasteVolumeModelPackageError(
                f"model package fileがありません、またはsymlinkです: {filename}"
            )

    sums = _read_sha256sums(package_path / "SHA256SUMS")
    if set(sums) != _PACKAGE_FILES:
        raise PasteVolumeModelPackageError(
            "SHA256SUMSはmanifest/model/preprocess/evaluationの全fileだけを含めてください"
        )
    for filename, expected in sums.items():
        actual = _sha256_file(package_path / filename)
        if actual != expected:
            raise PasteVolumeModelPackageError(
                f"model package checksumが一致しません: {filename}"
            )

    manifest = _read_json_object(package_path / "manifest.json", description="manifest")
    preprocess = _read_json_object(
        package_path / "preprocess.json", description="preprocess"
    )
    evaluation = _read_json_object(
        package_path / "evaluation.json", description="evaluation"
    )
    if manifest.get("schema_version") != MODEL_PACKAGE_SCHEMA_VERSION:
        raise PasteVolumeModelPackageError("未知のmodel package schemaです")
    status = manifest.get("status")
    if status not in {"candidate", "promoted"}:
        raise PasteVolumeModelPackageError("model package statusが不正です")
    if require_promoted and status != "promoted":
        raise PasteVolumeModelPackageError(
            "candidate model packageをproductionへloadできません"
        )
    files = _required_mapping(manifest, "files")
    for filename in ("model.onnx", "preprocess.json", "evaluation.json"):
        if files.get(filename) != sums[filename]:
            raise PasteVolumeModelPackageError(
                f"manifest file fingerprintが一致しません: {filename}"
            )
    model = _required_mapping(manifest, "model")
    if model.get("artifact_sha256") != sums["model.onnx"]:
        raise PasteVolumeModelPackageError("model artifact fingerprintが一致しません")
    if model.get("format") not in {"onnx-fp32", "onnx-int8-qdq"}:
        raise PasteVolumeModelPackageError("未知のmodel formatです")
    expected_model_id = f"{model['format']}:{sums['model.onnx'][:16]}"
    if model.get("id") != expected_model_id:
        raise PasteVolumeModelPackageError("model idがartifact fingerprintと不一致です")
    _validate_release_metadata_contract(manifest)
    _validate_preprocess_schema(preprocess, manifest)
    _validate_input_contract(manifest)
    _validate_training_coverage(manifest)
    _validate_manifest_lineage(manifest)
    _validate_training_weights_attestation(manifest)
    _validate_compile_parity_evidence(manifest, evaluation)
    _validate_export_parity_evidence(manifest, evaluation)
    if status == "promoted":
        _validate_promotion_evidence(manifest, evaluation)
    else:
        _validate_candidate_evidence(manifest, evaluation)
    return VerifiedModelPackage(
        path=package_path,
        manifest=manifest,
        preprocess=preprocess,
        evaluation=evaluation,
        model_sha256=sums["model.onnx"],
    )


def _validate_release_metadata_contract(manifest: Mapping[str, Any]) -> None:
    calibration = _required_mapping(manifest, "uncertainty_calibration")
    if set(calibration) != {"kind", "log_variance_offset"}:
        raise PasteVolumeModelPackageError("uncertainty calibration key集合が不正です")
    if calibration.get("kind") != "log-variance-offset" or not _is_finite_number(
        calibration.get("log_variance_offset")
    ):
        raise PasteVolumeModelPackageError("uncertainty calibration offsetが不正です")

    provenance = _required_mapping(manifest, "provenance")
    if set(provenance) != {"export", "artifact"}:
        raise PasteVolumeModelPackageError("model provenance key集合が不正です")
    export = _required_mapping(provenance, "export")
    if set(export) != {
        "api",
        "mode",
        "torch_version",
        "onnx_version",
        "opset_version",
    }:
        raise PasteVolumeModelPackageError("export provenance key集合が不正です")
    if (
        export.get("api") != "torch.onnx.export"
        or export.get("mode") != "dynamo"
        or not isinstance(export.get("torch_version"), str)
        or not export["torch_version"]
        or not _version_tuple(export["torch_version"])
        or not isinstance(export.get("onnx_version"), str)
        or not export["onnx_version"]
        or not _version_tuple(export["onnx_version"])
        or type(export.get("opset_version")) is not int
        or int(export["opset_version"]) < 18
    ):
        raise PasteVolumeModelPackageError("export provenanceが不正です")
    artifact = _required_mapping(provenance, "artifact")
    if set(artifact) != {"role", "tool", "version"}:
        raise PasteVolumeModelPackageError("artifact provenance key集合が不正です")
    model = _required_mapping(manifest, "model")
    expected_role = (
        "optimized-fp32" if model.get("format") == "onnx-fp32" else "int8-qdq"
    )
    if (
        artifact.get("role") != expected_role
        or artifact.get("tool") != "onnxruntime"
        or not isinstance(artifact.get("version"), str)
        or not artifact["version"]
        or not _version_tuple(artifact["version"])
    ):
        raise PasteVolumeModelPackageError("artifact provenanceが不正です")

    runtime = _required_mapping(manifest, "runtime")
    expected_quantization = (
        "none" if model.get("format") == "onnx-fp32" else "static-int8-qdq"
    )
    if (
        runtime.get("name") != "onnxruntime"
        or runtime.get("exporter") != "torch.onnx.export(dynamo=True)"
        or runtime.get("opset_version") != export.get("opset_version")
        or runtime.get("quantization") != expected_quantization
    ):
        raise PasteVolumeModelPackageError("runtime provenanceがmodelと不一致です")
    minimum_version = _required_string(runtime, "minimum_version")
    if not _version_tuple(minimum_version):
        raise PasteVolumeModelPackageError("runtime minimum versionが不正です")


def _validate_embedded_release_metadata(
    manifest: Mapping[str, Any], custom_metadata: Mapping[str, str]
) -> None:
    raw = custom_metadata.get("pcbasm.paste_volume.export")
    if raw is None:
        raise PasteVolumeModelPackageError(
            "ONNX modelにrelease provenance metadataがありません"
        )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PasteVolumeModelPackageError(
            "ONNX release provenance metadataが不正です"
        ) from exc
    if not isinstance(value, dict):
        raise PasteVolumeModelPackageError(
            "ONNX release provenance metadataはobjectが必要です"
        )
    metadata = cast(Mapping[str, Any], value)
    if (
        metadata.get("kind") != "pcbasm-paste-volume-onnx-export"
        or metadata.get("schema_version") != 1
    ):
        raise PasteVolumeModelPackageError(
            "ONNX release provenance kind/schemaが不正です"
        )
    calibration = _required_mapping(manifest, "uncertainty_calibration")
    if metadata.get("uncertainty_log_variance_offset") != calibration.get(
        "log_variance_offset"
    ):
        raise PasteVolumeModelPackageError(
            "uncertainty calibration offsetがONNX metadataと不一致です"
        )
    provenance = _required_mapping(manifest, "provenance")
    if metadata.get("export_provenance") != dict(
        _required_mapping(provenance, "export")
    ):
        raise PasteVolumeModelPackageError(
            "export provenanceがONNX metadataと不一致です"
        )
    if metadata.get("artifact_provenance") != dict(
        _required_mapping(provenance, "artifact")
    ):
        raise PasteVolumeModelPackageError(
            "artifact provenanceがONNX metadataと不一致です"
        )
    artifact = _required_mapping(provenance, "artifact")
    if metadata.get("artifact_role") != artifact.get("role"):
        raise PasteVolumeModelPackageError("artifact roleがONNX metadataと不一致です")
    lineage = _required_mapping(manifest, "lineage")
    if metadata.get("lineage") != dict(lineage):
        raise PasteVolumeModelPackageError(
            "training lineageがONNX metadataと不一致です"
        )
    if metadata.get("training_weights_attestation") != dict(
        _required_mapping(manifest, "training_weights_attestation")
    ):
        raise PasteVolumeModelPackageError(
            "formal training weights attestationがONNX metadataと不一致です"
        )
    compile_parity = _required_mapping(manifest, "compile_parity")
    compile_provenance = _required_mapping(compile_parity, "provenance")
    if metadata.get("source_weights_sha256") != compile_provenance.get(
        "weights_sha256"
    ):
        raise PasteVolumeModelPackageError(
            "compile parity source weightsがONNX metadataと不一致です"
        )
    export_parity = _required_mapping(manifest, "export_parity")
    if metadata.get("source_weights_sha256") != export_parity.get("weights_sha256"):
        raise PasteVolumeModelPackageError(
            "export parity source weightsがONNX metadataと不一致です"
        )


def _validate_candidate_evidence(
    manifest: Mapping[str, Any], evaluation: Mapping[str, Any]
) -> None:
    if set(evaluation) != {
        "schema_version",
        "validation",
        "compile_parity",
        "export_parity",
    }:
        raise PasteVolumeModelPackageError("candidate evaluation schemaが不正です")
    if evaluation.get("schema_version") != EVALUATION_SCHEMA_VERSION:
        raise PasteVolumeModelPackageError("未知のevaluation schemaです")
    if "cross_validation" in manifest:
        raise PasteVolumeModelPackageError(
            "candidate manifestにcross-validation promotion evidenceが含まれています"
        )
    validation = _required_mapping(evaluation, "validation")
    _validate_phase_evidence(
        manifest, validation, phase_name="validation", expected_phase="validation"
    )
    threshold = manifest.get("uncertainty_relative_std_threshold")
    if validation.get("uncertainty_relative_std_threshold") != threshold:
        raise PasteVolumeModelPackageError("manifest thresholdがvalidationと不一致です")
    _validate_release_evaluation_summary(manifest, {"validation": validation})


def _validate_manifest_lineage(manifest: Mapping[str, Any]) -> None:
    lineage = _required_mapping(manifest, "lineage")
    if set(lineage) != {
        "dataset_fingerprint",
        "split_fingerprint",
        "training_protocol_fingerprint",
        "source_run_id",
        "source_checkpoint_sha256",
        "source_checkpoint_role",
        "parent_run_id",
        "parent_checkpoint_id",
    }:
        raise PasteVolumeModelPackageError("training lineage key集合が不正です")
    for name in (
        "dataset_fingerprint",
        "split_fingerprint",
        "source_run_id",
        "source_checkpoint_sha256",
        "source_checkpoint_role",
        "training_protocol_fingerprint",
    ):
        _required_string(lineage, name)
    if lineage.get("source_checkpoint_role") != "best":
        raise PasteVolumeModelPackageError("source checkpoint roleはbestが必要です")
    for name in (
        "dataset_fingerprint",
        "split_fingerprint",
        "training_protocol_fingerprint",
        "source_checkpoint_sha256",
    ):
        if not _is_prefixed_sha256(lineage.get(name)):
            raise PasteVolumeModelPackageError(f"training lineage {name}が不正です")
    parent_run = lineage.get("parent_run_id")
    parent_checkpoint = lineage.get("parent_checkpoint_id")
    if (parent_run is None) != (parent_checkpoint is None):
        raise PasteVolumeModelPackageError("fine-tune parent lineageが片方だけです")
    if parent_run is not None:
        _required_string(lineage, "parent_run_id")
        _required_string(lineage, "parent_checkpoint_id")


def _validate_training_weights_attestation(manifest: Mapping[str, Any]) -> None:
    raw = _required_mapping(manifest, "training_weights_attestation")
    try:
        attestation = FormalArtifactAttestation.from_dict(raw)
    except ValueError as exc:
        raise PasteVolumeModelPackageError(
            "formal training weights attestationが不正です"
        ) from exc
    lineage = _required_mapping(manifest, "lineage")
    compile_parity = _required_mapping(manifest, "compile_parity")
    provenance = _required_mapping(compile_parity, "provenance")
    if (
        attestation.status != "FINISHED"
        or attestation.output_kind != "file"
        or attestation.run_kind not in ("base-train", "finetune")
        or attestation.run_id != lineage.get("source_run_id")
        or attestation.output_fingerprint != provenance.get("weights_sha256")
    ):
        raise PasteVolumeModelPackageError(
            "formal training weights attestationがrelease lineageと不一致です"
        )


def _validate_compile_parity_evidence(
    manifest: Mapping[str, Any], evaluation: Mapping[str, Any]
) -> None:
    report = _required_mapping(manifest, "compile_parity")
    evaluation_report = _required_mapping(evaluation, "compile_parity")
    if dict(report) != dict(evaluation_report):
        raise PasteVolumeModelPackageError(
            "compile parity evidenceがmanifestとevaluationで不一致です"
        )
    expected_report_keys = {
        "kind",
        "schema_version",
        "created_at_utc",
        "success",
        "provenance",
        "runtime",
        "cases",
        "content_sha256",
    }
    if set(report) != expected_report_keys:
        raise PasteVolumeModelPackageError("compile parity report key集合が不正です")
    if (
        report.get("kind") != "pcbasm-paste-volume-compile-parity"
        or report.get("schema_version") != 1
        or report.get("success") is not True
    ):
        raise PasteVolumeModelPackageError(
            "compile parity reportのkind/schema/successが不正です"
        )
    timestamp = report.get("created_at_utc")
    if not isinstance(timestamp, str):
        raise PasteVolumeModelPackageError("compile parity timestampが不正です")
    try:
        created_at = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise PasteVolumeModelPackageError(
            "compile parity timestampがISO-8601ではありません"
        ) from exc
    if created_at.tzinfo is None or created_at.utcoffset() != timedelta(0):
        raise PasteVolumeModelPackageError("compile parity timestampはUTCが必要です")
    content_sha256 = report.get("content_sha256")
    if not _is_prefixed_sha256(content_sha256):
        raise PasteVolumeModelPackageError("compile parity content hashが不正です")
    content = dict(report)
    del content["content_sha256"]
    expected_hash = "sha256:" + _canonical_json_sha256(content)
    if content_sha256 != expected_hash:
        raise PasteVolumeModelPackageError(
            "compile parity content hashが内容と一致しません"
        )

    provenance = _required_mapping(report, "provenance")
    if set(provenance) != {
        "weights_sha256",
        "training_protocol_fingerprint",
        "dataset_fingerprint",
        "split_fingerprint",
    } or any(not _is_prefixed_sha256(value) for value in provenance.values()):
        raise PasteVolumeModelPackageError("compile parity provenanceが不正です")
    lineage = _required_mapping(manifest, "lineage")
    for provenance_name, lineage_name in (
        ("training_protocol_fingerprint", "training_protocol_fingerprint"),
        ("dataset_fingerprint", "dataset_fingerprint"),
        ("split_fingerprint", "split_fingerprint"),
    ):
        if provenance.get(provenance_name) != lineage.get(lineage_name):
            raise PasteVolumeModelPackageError(
                f"compile parity lineageが不一致です: {provenance_name}"
            )

    runtime = _required_mapping(report, "runtime")
    if set(runtime) != {
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
    }:
        raise PasteVolumeModelPackageError("compile parity runtime key集合が不正です")
    for name in ("backend", "mode", "device", "torch_version"):
        _required_string(runtime, name)
    if (
        runtime.get("backend") != "inductor"
        or runtime.get("mode") != "default"
        or runtime.get("graph_break_count") != 0
        or not re.fullmatch(r"(?:cpu|cuda)(?::[0-9]+)?", str(runtime.get("device")))
        or runtime.get("dtype") != "float32"
        or runtime.get("tolerance_profile") != "fp32-v1"
        or type(runtime.get("fullgraph")) is not bool
        or (
            runtime.get("dynamic") is not None
            and type(runtime.get("dynamic")) is not bool
        )
    ):
        raise PasteVolumeModelPackageError("compile parity runtimeが不正です")
    for name in ("rtol", "atol", "compile_setup_seconds"):
        if not _is_nonnegative_finite(runtime.get(name)):
            raise PasteVolumeModelPackageError(
                f"compile parity runtime {name}が不正です"
            )
    if float(runtime["rtol"]) + float(runtime["atol"]) <= 0:
        raise PasteVolumeModelPackageError("compile parity toleranceが不正です")

    cases = report.get("cases")
    if not isinstance(cases, list) or len(cases) != len(_COMPILE_CASES):
        raise PasteVolumeModelPackageError("compile parityは固定5 shapeが必要です")
    expected_case_keys = {
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
    numeric_case_keys = expected_case_keys - {
        "case_id",
        "shape_case",
        "input_shape",
        "sample_ids",
        "compile_succeeded",
        "parity_passed",
        "missing_gradient_parameters",
        "nonfinite_gradient_parameters",
        "mismatched_gradient_parameters",
        "trainable_parameter_count",
        "checked_gradient_count",
        "failure_reason",
    }
    seen_cases: set[str] = set()
    seen_ids: set[str] = set()
    for index, raw_case in enumerate(cases):
        if not isinstance(raw_case, dict) or set(raw_case) != expected_case_keys:
            raise PasteVolumeModelPackageError(
                f"compile parity case[{index}]のkey集合が不正です"
            )
        case = cast(Mapping[str, Any], raw_case)
        case_id = _required_string(case, "case_id")
        shape_case = case.get("shape_case")
        if (
            not isinstance(shape_case, str)
            or shape_case not in _COMPILE_CASES
            or shape_case in seen_cases
            or case_id in seen_ids
        ):
            raise PasteVolumeModelPackageError("compile parity case identityが不正です")
        seen_cases.add(shape_case)
        seen_ids.add(case_id)
        shape = case.get("input_shape")
        if (
            not isinstance(shape, list)
            or len(shape) != 4
            or any(type(value) is not int or value < 1 for value in shape)
            or shape[1] != 6
        ):
            raise PasteVolumeModelPackageError("compile parity input shapeが不正です")
        fixed = _COMPILE_SHAPES.get(shape_case)
        if fixed is not None and tuple(shape[2:]) != fixed:
            raise PasteVolumeModelPackageError("compile parity固定shapeが不一致です")
        sample_ids = case.get("sample_ids")
        if (
            not isinstance(sample_ids, list)
            or len(sample_ids) != shape[0]
            or any(not isinstance(item, str) or not item for item in sample_ids)
            or len(set(sample_ids)) != len(sample_ids)
        ):
            raise PasteVolumeModelPackageError("compile parity sample IDsが不正です")
        parameter_count = case.get("trainable_parameter_count")
        checked_count = case.get("checked_gradient_count")
        if (
            case.get("compile_succeeded") is not True
            or case.get("parity_passed") is not True
            or type(parameter_count) is not int
            or parameter_count < 1
            or checked_count != parameter_count
            or case.get("failure_reason") is not None
            or any(
                case.get(name) != []
                for name in (
                    "missing_gradient_parameters",
                    "nonfinite_gradient_parameters",
                    "mismatched_gradient_parameters",
                )
            )
        ):
            raise PasteVolumeModelPackageError("compile parity case gateが失敗です")
        if any(
            not _is_nonnegative_finite(case.get(name)) for name in numeric_case_keys
        ):
            raise PasteVolumeModelPackageError("compile parity case計測値が不正です")
    if seen_cases != set(_COMPILE_CASES):
        raise PasteVolumeModelPackageError("compile parity shape coverageが不正です")


def _validate_export_parity_evidence(
    manifest: Mapping[str, Any], evaluation: Mapping[str, Any]
) -> None:
    report = _required_mapping(manifest, "export_parity")
    evaluation_report = _required_mapping(evaluation, "export_parity")
    if dict(report) != dict(evaluation_report):
        raise PasteVolumeModelPackageError(
            "manifest/evaluationのexport parity evidenceが一致しません"
        )
    expected_keys = {
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
    if (
        set(report) != expected_keys
        or report.get("kind") != "pcbasm-paste-volume-export-parity"
        or report.get("schema_version") != 1
        or report.get("split_name") != "validation"
        or report.get("success") is not True
    ):
        raise PasteVolumeModelPackageError("export parity report schema/gateが不正です")
    content_sha256 = report.get("content_sha256")
    if not _is_prefixed_sha256(content_sha256):
        raise PasteVolumeModelPackageError("export parity content hashが不正です")
    content = dict(report)
    del content["content_sha256"]
    if content_sha256 != "sha256:" + _canonical_json_sha256(content):
        raise PasteVolumeModelPackageError(
            "export parity content hashが内容と一致しません"
        )
    if not _is_prefixed_sha256(report.get("weights_sha256")) or not _is_sha256(
        report.get("fp32_model_artifact_sha256")
    ):
        raise PasteVolumeModelPackageError("export parity artifact hashが不正です")
    for name in (
        "training_protocol_fingerprint",
        "dataset_fingerprint",
        "split_fingerprint",
    ):
        if not _is_prefixed_sha256(report.get(name)):
            raise PasteVolumeModelPackageError(f"export parity {name}が不正です")
    validation = _required_mapping(evaluation, "validation")
    lineage = _required_mapping(manifest, "lineage")
    checks = (
        (
            report.get("fp32_model_artifact_sha256"),
            validation.get("fp32_reference_artifact_sha256"),
        ),
        (
            report.get("training_protocol_fingerprint"),
            lineage.get("training_protocol_fingerprint"),
        ),
        (report.get("dataset_fingerprint"), validation.get("dataset_fingerprint")),
        (report.get("split_fingerprint"), validation.get("split_fingerprint")),
        (report.get("sample_ids"), validation.get("evaluated_sample_ids")),
        (
            report.get("training_sample_ids"),
            validation.get("training_sample_ids"),
        ),
    )
    if any(actual != expected for actual, expected in checks):
        raise PasteVolumeModelPackageError(
            "export parity validation assignment/lineageが不一致です"
        )
    sample_ids = _required_string_array(report, "sample_ids")
    training_ids = _required_string_array(report, "training_sample_ids")
    if (
        sample_ids != tuple(sorted(sample_ids))
        or len(set(sample_ids)) != len(sample_ids)
        or training_ids != tuple(sorted(training_ids))
        or len(set(training_ids)) != len(training_ids)
    ):
        raise PasteVolumeModelPackageError("export parity sample assignmentが不正です")
    raw_results = report.get("sample_results")
    if not isinstance(raw_results, list) or len(raw_results) != len(sample_ids):
        raise PasteVolumeModelPackageError("export parity sample resultsが不正です")
    result_keys = {
        "sample_id",
        "mean_absolute_error_ul",
        "mean_tolerance_ul",
        "log_variance_absolute_error",
        "log_variance_tolerance",
        "passed",
    }
    mean_errors: list[float] = []
    log_variance_errors: list[float] = []
    for index, raw_result in enumerate(raw_results):
        if not isinstance(raw_result, dict) or set(raw_result) != result_keys:
            raise PasteVolumeModelPackageError(
                f"export parity sample result[{index}]が不正です"
            )
        if raw_result.get("sample_id") != sample_ids[index]:
            raise PasteVolumeModelPackageError(
                "export parity sample result順序がassignmentと不一致です"
            )
        for name in (
            "mean_absolute_error_ul",
            "mean_tolerance_ul",
            "log_variance_absolute_error",
            "log_variance_tolerance",
        ):
            if not _is_nonnegative_finite(raw_result.get(name)):
                raise PasteVolumeModelPackageError(
                    f"export parity sample {name}が不正です"
                )
        if (
            float(raw_result["mean_tolerance_ul"]) <= 0
            or float(raw_result["log_variance_tolerance"]) <= 0
            or raw_result.get("passed") is not True
            or float(raw_result["mean_absolute_error_ul"])
            > float(raw_result["mean_tolerance_ul"])
            or float(raw_result["log_variance_absolute_error"])
            > float(raw_result["log_variance_tolerance"])
        ):
            raise PasteVolumeModelPackageError("export parity sample gateが失敗です")
        mean_errors.append(float(raw_result["mean_absolute_error_ul"]))
        log_variance_errors.append(float(raw_result["log_variance_absolute_error"]))
    raw_shape_results = report.get("shape_results")
    if not isinstance(raw_shape_results, list) or len(raw_shape_results) != len(
        _COMPILE_SHAPES
    ):
        raise PasteVolumeModelPackageError(
            "export parity dynamic shape resultsが不正です"
        )
    shape_result_keys = {
        "case_name",
        "image_height",
        "image_width",
        "mean_absolute_error_ul",
        "mean_tolerance_ul",
        "log_variance_absolute_error",
        "log_variance_tolerance",
        "passed",
    }
    expected_shapes = tuple(_COMPILE_SHAPES.items())
    for index, raw_result in enumerate(raw_shape_results):
        if not isinstance(raw_result, dict) or set(raw_result) != shape_result_keys:
            raise PasteVolumeModelPackageError(
                f"export parity dynamic shape result[{index}]が不正です"
            )
        case_name, shape = expected_shapes[index]
        if (
            raw_result.get("case_name") != case_name
            or raw_result.get("image_height") != shape[0]
            or raw_result.get("image_width") != shape[1]
        ):
            raise PasteVolumeModelPackageError(
                "export parity dynamic shape coverageが不正です"
            )
        for name in (
            "mean_absolute_error_ul",
            "mean_tolerance_ul",
            "log_variance_absolute_error",
            "log_variance_tolerance",
        ):
            if not _is_nonnegative_finite(raw_result.get(name)):
                raise PasteVolumeModelPackageError(
                    f"export parity dynamic shape {name}が不正です"
                )
        if (
            float(raw_result["mean_tolerance_ul"]) <= 0
            or float(raw_result["log_variance_tolerance"]) <= 0
            or raw_result.get("passed") is not True
            or float(raw_result["mean_absolute_error_ul"])
            > float(raw_result["mean_tolerance_ul"])
            or float(raw_result["log_variance_absolute_error"])
            > float(raw_result["log_variance_tolerance"])
        ):
            raise PasteVolumeModelPackageError(
                "export parity dynamic shape gateが失敗です"
            )
        mean_errors.append(float(raw_result["mean_absolute_error_ul"]))
        log_variance_errors.append(float(raw_result["log_variance_absolute_error"]))
    if report.get("maximum_mean_absolute_error_ul") != max(mean_errors) or report.get(
        "maximum_log_variance_absolute_error"
    ) != max(log_variance_errors):
        raise PasteVolumeModelPackageError("export parity aggregateが不一致です")


def _validate_benchmark_evidence(benchmark: Mapping[str, Any]) -> None:
    expected_keys = {
        "schema_version",
        "candidate_id",
        "model_artifact_sha256",
        "source_run_id",
        "source_checkpoint_sha256",
        "source_checkpoint_role",
        "training_dataset_fingerprint",
        "training_split_fingerprint",
        "training_protocol_fingerprint",
        "dataset_fingerprint",
        "split_fingerprint",
        "benchmark_sample_ids",
        "parent_run_id",
        "parent_checkpoint_id",
        "platform_model",
        "is_raspberry_pi_5",
        "cold_latency_ms",
        "p50_latency_ms",
        "p95_latency_ms",
        "p99_latency_ms",
        "peak_rss_bytes",
        "artifact_size_bytes",
        "sample_count",
        "warmup_iterations",
        "measured_iterations",
        "category_results",
        "cold_start_clock",
        "cold_start_origin",
        "os_id",
        "os_release",
        "python_version",
        "onnxruntime_version",
        "cpu_governor",
        "platform_machine",
        "power_condition",
        "cooling_condition",
    }
    if set(benchmark) != expected_keys or benchmark.get("schema_version") != 1:
        raise PasteVolumeModelPackageError("benchmark schema/key集合が不正です")
    if (
        benchmark.get("is_raspberry_pi_5") is not True
        or "raspberry pi 5" not in _required_string(benchmark, "platform_model").lower()
        or _required_string(benchmark, "platform_machine").lower() != "aarch64"
    ):
        raise PasteVolumeModelPackageError("Raspberry Pi 5/aarch64実測が必要です")
    for name in (
        "cold_start_origin",
        "os_id",
        "os_release",
        "python_version",
        "onnxruntime_version",
        "cpu_governor",
        "power_condition",
        "cooling_condition",
    ):
        _required_string(benchmark, name)
    if benchmark.get("cold_start_clock") != "CLOCK_MONOTONIC":
        raise PasteVolumeModelPackageError("benchmark cold-start clockが不正です")
    if benchmark.get("warmup_iterations") != 10:
        raise PasteVolumeModelPackageError("benchmark warm-upは10回が必要です")
    if benchmark.get("measured_iterations") != 100:
        raise PasteVolumeModelPackageError("benchmark測定は100回が必要です")
    if benchmark.get("sample_count") != 5:
        raise PasteVolumeModelPackageError("benchmarkは5 categoryが必要です")
    for name in (
        "cold_latency_ms",
        "p50_latency_ms",
        "p95_latency_ms",
        "p99_latency_ms",
    ):
        if not _is_nonnegative_finite(benchmark.get(name)):
            raise PasteVolumeModelPackageError(f"benchmark {name}が不正です")
    category_values = benchmark.get("category_results")
    if not isinstance(category_values, list) or len(category_values) != 5:
        raise PasteVolumeModelPackageError("benchmark category_resultsが不正です")
    category_keys = {
        "category",
        "sample_id",
        "image_height",
        "image_width",
        "warmup_iterations",
        "measured_iterations",
        "p50_latency_ms",
        "p95_latency_ms",
        "p99_latency_ms",
    }
    results: list[Mapping[str, Any]] = []
    for index, raw_result in enumerate(category_values):
        if not isinstance(raw_result, dict) or set(raw_result) != category_keys:
            raise PasteVolumeModelPackageError(
                f"benchmark category[{index}]のkey集合が不正です"
            )
        result = cast(Mapping[str, Any], raw_result)
        if result.get("category") != _BENCHMARK_CATEGORIES[index]:
            raise PasteVolumeModelPackageError("benchmark category順が不正です")
        _required_string(result, "sample_id")
        if (
            type(result.get("image_height")) is not int
            or int(result["image_height"]) < 1
            or type(result.get("image_width")) is not int
            or int(result["image_width"]) < 1
            or result.get("warmup_iterations") != 10
            or result.get("measured_iterations") != 100
        ):
            raise PasteVolumeModelPackageError("benchmark category計測契約が不正です")
        for name in ("p50_latency_ms", "p95_latency_ms", "p99_latency_ms"):
            if not _is_nonnegative_finite(result.get(name)):
                raise PasteVolumeModelPackageError(
                    f"benchmark category {name}が不正です"
                )
        if float(result["p95_latency_ms"]) > 1000.0:
            raise PasteVolumeModelPackageError(
                f"benchmark category p95 gate失敗です: {result['category']}"
            )
        results.append(result)
    category_sample_ids = tuple(str(result["sample_id"]) for result in results)
    if (
        benchmark.get("benchmark_sample_ids") != list(category_sample_ids)
        or len(set(category_sample_ids)) != 5
    ):
        raise PasteVolumeModelPackageError("benchmark category sample証跡が不正です")
    if benchmark.get("p95_latency_ms") != max(
        result["p95_latency_ms"] for result in results
    ) or benchmark.get("p99_latency_ms") != max(
        result["p99_latency_ms"] for result in results
    ):
        raise PasteVolumeModelPackageError("benchmark aggregate latencyが不一致です")


def _validate_promotion_evidence(
    manifest: Mapping[str, Any], evaluation: Mapping[str, Any]
) -> None:
    promotion = _required_mapping(manifest, "promotion")
    if set(evaluation) != {
        "schema_version",
        "validation",
        "frozen_test",
        "benchmark",
        "compile_parity",
        "export_parity",
        "cross_validation",
    }:
        raise PasteVolumeModelPackageError("promoted evaluation schemaが不正です")
    if evaluation.get("schema_version") != EVALUATION_SCHEMA_VERSION:
        raise PasteVolumeModelPackageError("未知のevaluation schemaです")
    _validate_cross_validation_evidence(manifest, evaluation)
    gate_spec = _required_mapping(promotion, "gate_spec")
    expected = {
        "primary_accuracy_max": 0.10,
        "fp32_relative_tolerance": 0.001,
        "fp32_absolute_tolerance_ul": 0.000001,
        "padding_degradation_max": 0.01,
        "int8_accuracy_degradation_max": 0.01,
        "int8_coverage_delta_max": 0.03,
        "pi_p95_latency_ms_max": 1000.0,
        "three_sample_acceptance_min": 0.90,
    }
    if dict(gate_spec) != expected:
        raise PasteVolumeModelPackageError("promotion gate定数が正規値と一致しません")
    selected_id = _required_string(promotion, "selected_candidate_id")
    model = _required_mapping(manifest, "model")
    if selected_id != model.get("id"):
        raise PasteVolumeModelPackageError("selected candidateとmodel idが一致しません")
    validation = _required_mapping(evaluation, "validation")
    frozen_test = _required_mapping(evaluation, "frozen_test")
    benchmark = _required_mapping(evaluation, "benchmark")
    _validate_phase_evidence(
        manifest, validation, phase_name="validation", expected_phase="validation"
    )
    _validate_phase_evidence(
        manifest,
        frozen_test,
        phase_name="frozen_test",
        expected_phase="frozen_test",
    )
    if benchmark.get("candidate_id") != selected_id:
        raise PasteVolumeModelPackageError(
            "benchmark candidate fingerprintが一致しません"
        )
    if benchmark.get("model_artifact_sha256") != model.get("artifact_sha256"):
        raise PasteVolumeModelPackageError(
            "benchmark artifact fingerprintが一致しません"
        )
    _validate_benchmark_evidence(benchmark)
    lineage = _required_mapping(manifest, "lineage")
    for name in (
        "dataset_fingerprint",
        "split_fingerprint",
        "source_run_id",
        "source_checkpoint_sha256",
        "source_checkpoint_role",
        "training_protocol_fingerprint",
    ):
        _required_string(lineage, name)
    if lineage.get("source_checkpoint_role") != "best":
        raise PasteVolumeModelPackageError("source checkpoint roleはbestが必要です")
    parent_run = lineage.get("parent_run_id")
    parent_checkpoint = lineage.get("parent_checkpoint_id")
    if (parent_run is None) != (parent_checkpoint is None):
        raise PasteVolumeModelPackageError("fine-tune parent lineageが片方だけです")
    if parent_run is not None:
        _required_string(lineage, "parent_run_id")
        _required_string(lineage, "parent_checkpoint_id")
    for name in (
        "source_run_id",
        "source_checkpoint_sha256",
        "source_checkpoint_role",
        "parent_run_id",
        "parent_checkpoint_id",
    ):
        if benchmark.get(name) != lineage.get(name):
            raise PasteVolumeModelPackageError(
                f"benchmark training lineageが不一致です: {name}"
            )
    if benchmark.get("training_dataset_fingerprint") != lineage.get(
        "dataset_fingerprint"
    ):
        raise PasteVolumeModelPackageError(
            "benchmark training dataset fingerprintが不一致です"
        )
    if benchmark.get("training_split_fingerprint") != lineage.get("split_fingerprint"):
        raise PasteVolumeModelPackageError(
            "benchmark training split fingerprintが不一致です"
        )
    if benchmark.get("training_protocol_fingerprint") != lineage.get(
        "training_protocol_fingerprint"
    ):
        raise PasteVolumeModelPackageError(
            "benchmark training protocol fingerprintが不一致です"
        )
    if benchmark.get("dataset_fingerprint") != validation.get(
        "dataset_fingerprint"
    ) or benchmark.get("split_fingerprint") != validation.get("split_fingerprint"):
        raise PasteVolumeModelPackageError(
            "benchmark release evaluation assignmentがvalidationと不一致です"
        )
    benchmark_sample_ids = _required_string_array(benchmark, "benchmark_sample_ids")
    validation_sample_ids = _required_string_array(validation, "evaluated_sample_ids")
    frozen_sample_ids = _required_string_array(frozen_test, "evaluated_sample_ids")
    training_sample_ids = _required_string_array(validation, "training_sample_ids")
    if _required_string_array(frozen_test, "training_sample_ids") != (
        training_sample_ids
    ):
        raise PasteVolumeModelPackageError(
            "frozen testのtraining sample証跡がvalidationと不一致です"
        )
    if not set(benchmark_sample_ids) <= set(validation_sample_ids):
        raise PasteVolumeModelPackageError(
            "benchmark sampleがvalidation assignment外です"
        )
    if set(validation_sample_ids) & set(frozen_sample_ids):
        raise PasteVolumeModelPackageError(
            "frozen testがvalidation sampleを再利用しています"
        )
    if set(training_sample_ids) & set(frozen_sample_ids):
        raise PasteVolumeModelPackageError(
            "frozen testがtraining sampleを再利用しています"
        )
    if frozen_test.get("fp32_reference_artifact_sha256") != validation.get(
        "fp32_reference_artifact_sha256"
    ):
        raise PasteVolumeModelPackageError(
            "frozen testのFP32 reference artifactがvalidationと不一致です"
        )
    _validate_release_evaluation_summary(
        manifest, {"validation": validation, "frozen_test": frozen_test}
    )
    threshold = manifest.get("uncertainty_relative_std_threshold")
    if validation.get("uncertainty_relative_std_threshold") != threshold:
        raise PasteVolumeModelPackageError("manifest thresholdがvalidationと不一致です")
    if frozen_test.get("uncertainty_relative_std_threshold") != threshold:
        raise PasteVolumeModelPackageError("frozen testでthresholdが変更されています")


def _validate_cross_validation_evidence(
    manifest: Mapping[str, Any], evaluation: Mapping[str, Any]
) -> None:
    manifest_summaries = manifest.get("cross_validation")
    evaluation_evidence = evaluation.get("cross_validation")
    if (
        not isinstance(manifest_summaries, list)
        or not isinstance(evaluation_evidence, list)
        or len(manifest_summaries) != 3
        or len(evaluation_evidence) != 3
    ):
        raise PasteVolumeModelPackageError(
            "cross-validation evidenceがmanifest/evaluationで不正です"
        )
    from .cross_validation import CrossValidationResult

    lineage = _required_mapping(manifest, "lineage")
    by_dimension: dict[str, CrossValidationResult] = {}
    run_ids: set[str] = set()
    tracking_uri_hashes: set[str] = set()
    expected_summary_keys = {
        "dimension",
        "report_fingerprint",
        "report_artifact_sha256",
        "summary_run_id",
        "tracking_uri_sha256",
        "attestation_sha256",
    }
    for index, raw_evidence in enumerate(evaluation_evidence):
        if not isinstance(raw_evidence, dict) or set(raw_evidence) != {
            "kind",
            "schema_version",
            "report",
            "summary_attestation",
        }:
            raise PasteVolumeModelPackageError(
                f"cross-validation promotion evidence[{index}]が不正です"
            )
        if (
            raw_evidence.get("kind")
            != "pcbasm-paste-volume-cross-validation-promotion-evidence"
            or raw_evidence.get("schema_version") != 1
        ):
            raise PasteVolumeModelPackageError(
                f"cross-validation promotion evidence[{index}] kind/schemaが不正です"
            )
        raw_report = raw_evidence.get("report")
        raw_attestation = raw_evidence.get("summary_attestation")
        if not isinstance(raw_report, dict) or not isinstance(raw_attestation, dict):
            raise PasteVolumeModelPackageError(
                f"cross-validation evidence[{index}] payloadが不正です"
            )
        try:
            attestation = FormalArtifactAttestation.from_dict(raw_attestation)
            report = CrossValidationResult.from_dict(
                raw_report,
                report_path=attestation.output_path,
            )
        except ValueError as exc:
            raise PasteVolumeModelPackageError(
                f"cross-validation evidence[{index}]が不正です: {exc}"
            ) from exc
        persisted_report = (
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
        report_artifact_sha256 = (
            "sha256:" + hashlib.sha256(persisted_report).hexdigest()
        )
        if (
            attestation.run_kind != "cross-validation-summary"
            or attestation.output_kind != "file"
            or attestation.output_fingerprint != report_artifact_sha256
        ):
            raise PasteVolumeModelPackageError(
                f"cross-validation formal attestation[{index}]がreportと不一致です"
            )
        summary = manifest_summaries[index]
        if not isinstance(summary, dict) or set(summary) != expected_summary_keys:
            raise PasteVolumeModelPackageError(
                f"cross-validation manifest summary[{index}]が不正です"
            )
        expected_summary = {
            "dimension": report.dimension,
            "report_fingerprint": report.report_fingerprint,
            "report_artifact_sha256": attestation.output_fingerprint,
            "summary_run_id": attestation.run_id,
            "tracking_uri_sha256": attestation.tracking_uri_sha256,
            "attestation_sha256": "sha256:"
            + _canonical_json_sha256(attestation.to_dict()),
        }
        if summary != expected_summary:
            raise PasteVolumeModelPackageError(
                f"cross-validation manifest summary[{index}]がevidenceと不一致です"
            )
        if report.dimension in by_dimension:
            raise PasteVolumeModelPackageError(
                "cross-validation dimensionが重複しています"
            )
        by_dimension[report.dimension] = report
        if attestation.run_id in run_ids:
            raise PasteVolumeModelPackageError(
                "cross-validation summary run IDが重複しています"
            )
        run_ids.add(attestation.run_id)
        tracking_uri_hashes.add(attestation.tracking_uri_sha256)
    if set(by_dimension) != {"machine", "paste_lot", "nozzle"}:
        raise PasteVolumeModelPackageError(
            "machine/paste_lot/nozzleの3 cross-validation証跡が必要です"
        )
    if len(tracking_uri_hashes) != 1:
        raise PasteVolumeModelPackageError(
            "cross-validation tracking URIが一致しません"
        )
    for dimension, report in by_dimension.items():
        if not report.available or report.reason is not None:
            raise PasteVolumeModelPackageError(
                f"cross-validationがavailableではありません: {dimension}"
            )
        if report.composite_fingerprint != lineage.get("dataset_fingerprint"):
            raise PasteVolumeModelPackageError(
                f"cross-validation dataset lineageが不一致です: {dimension}"
            )
        if report.protocol_fingerprint != lineage.get("training_protocol_fingerprint"):
            raise PasteVolumeModelPackageError(
                f"cross-validation protocol lineageが不一致です: {dimension}"
            )


def _validate_phase_evidence(
    manifest: Mapping[str, Any],
    phase: Mapping[str, Any],
    *,
    phase_name: str,
    expected_phase: str,
) -> None:
    model = _required_mapping(manifest, "model")
    if phase.get("phase") != expected_phase:
        raise PasteVolumeModelPackageError(f"{phase_name} evaluation phaseが不正です")
    if phase.get("candidate_id") != model.get("id"):
        raise PasteVolumeModelPackageError(
            f"{phase_name} candidate fingerprintが一致しません"
        )
    if phase.get("model_format") != model.get("format"):
        raise PasteVolumeModelPackageError(f"{phase_name} model formatが不一致です")
    if phase.get("model_artifact_sha256") != model.get("artifact_sha256"):
        raise PasteVolumeModelPackageError(
            f"{phase_name} model artifact fingerprintが一致しません"
        )
    _required_string(phase, "fp32_reference_artifact_sha256")
    if phase.get("gate_passed") is not True or phase.get("gate_failures") != []:
        raise PasteVolumeModelPackageError(f"{phase_name} gateを通過していません")
    _validate_evaluation_numbers(phase, phase_name=phase_name)

    lineage = _required_mapping(manifest, "lineage")
    if phase.get("training_dataset_fingerprint") != lineage.get("dataset_fingerprint"):
        raise PasteVolumeModelPackageError(
            f"{phase_name} training dataset fingerprintが不一致です"
        )
    if phase.get("training_split_fingerprint") != lineage.get("split_fingerprint"):
        raise PasteVolumeModelPackageError(
            f"{phase_name} training split fingerprintが不一致です"
        )
    if phase.get("training_protocol_fingerprint") != lineage.get(
        "training_protocol_fingerprint"
    ):
        raise PasteVolumeModelPackageError(
            f"{phase_name} training protocol fingerprintが不一致です"
        )
    for name in (
        "source_run_id",
        "source_checkpoint_sha256",
        "source_checkpoint_role",
        "parent_run_id",
        "parent_checkpoint_id",
    ):
        if phase.get(name) != lineage.get(name):
            raise PasteVolumeModelPackageError(
                f"{phase_name} training lineageが不一致です: {name}"
            )
    _required_string(phase, "dataset_fingerprint")
    _required_string(phase, "split_fingerprint")
    evaluated_ids = _required_string_array(phase, "evaluated_sample_ids")
    training_ids = _required_string_array(phase, "training_sample_ids")
    if len(evaluated_ids) != phase.get("sample_count"):
        raise PasteVolumeModelPackageError(f"{phase_name} sample証跡数が不一致です")
    if set(evaluated_ids) & set(training_ids):
        raise PasteVolumeModelPackageError(
            f"{phase_name}がtraining sampleを再利用しています"
        )


def _validate_release_evaluation_summary(
    manifest: Mapping[str, Any], phases: Mapping[str, Mapping[str, Any]]
) -> None:
    summary = _required_mapping(manifest, "release_evaluation")
    if set(summary) != set(phases):
        raise PasteVolumeModelPackageError("release evaluation phase集合が不一致です")
    for phase_name, phase in phases.items():
        item = _required_mapping(summary, phase_name)
        if set(item) != {
            "dataset_fingerprint",
            "split_fingerprint",
            "sample_count",
            "sample_ids_sha256",
        }:
            raise PasteVolumeModelPackageError(
                f"{phase_name} release evaluation summaryが不正です"
            )
        sample_ids = _required_string_array(phase, "evaluated_sample_ids")
        expected_hash = _canonical_json_sha256({"sample_ids": sorted(sample_ids)})
        if (
            item.get("dataset_fingerprint") != phase.get("dataset_fingerprint")
            or item.get("split_fingerprint") != phase.get("split_fingerprint")
            or item.get("sample_count") != len(sample_ids)
            or item.get("sample_ids_sha256") != expected_hash
        ):
            raise PasteVolumeModelPackageError(
                f"{phase_name} release evaluation summaryがreportと不一致です"
            )


def _validate_preprocess_schema(
    preprocess: Mapping[str, Any], manifest: Mapping[str, Any]
) -> None:
    if set(preprocess) != {
        "schema_version",
        "channel_order",
        "input_channels",
        "normalization",
        "image_constraints",
    }:
        raise PasteVolumeModelPackageError("preprocess schema key集合が不正です")
    if preprocess.get("schema_version") != 1:
        raise PasteVolumeModelPackageError("未知のpreprocess schemaです")
    if preprocess.get("channel_order") != "RGB":
        raise PasteVolumeModelPackageError("preprocess channel_orderはRGBが必要です")
    if preprocess.get("input_channels") != 6:
        raise PasteVolumeModelPackageError("preprocess input_channelsは6が必要です")
    normalization = _required_mapping(preprocess, "normalization")
    if set(normalization) != {
        "kind",
        "axes",
        "affine",
        "per_channel",
        "epsilon",
    }:
        raise PasteVolumeModelPackageError("normalization schema key集合が不正です")
    if normalization.get("kind") != "sample-layer-norm":
        raise PasteVolumeModelPackageError("sample単位LayerNormが必要です")
    if normalization.get("per_channel") is not False:
        raise PasteVolumeModelPackageError("channel別standardizationは許可されません")
    if normalization.get("axes") != [0, 1, 2]:
        raise PasteVolumeModelPackageError("SampleLayerNorm axesは[0,1,2]が必要です")
    if normalization.get("affine") is not False:
        raise PasteVolumeModelPackageError("SampleLayerNorm affineはfalseが必要です")
    epsilon = normalization.get("epsilon")
    if not _is_positive_finite(epsilon):
        raise PasteVolumeModelPackageError("normalization epsilonが不正です")
    constraints = _required_mapping(preprocess, "image_constraints")
    if set(constraints) != {
        "min_size",
        "max_size",
        "max_pixels",
        "stride",
        "normalization_epsilon",
    }:
        raise PasteVolumeModelPackageError("image constraints key集合が不正です")
    min_size = constraints.get("min_size")
    max_size = constraints.get("max_size")
    max_pixels = constraints.get("max_pixels")
    stride = constraints.get("stride")
    constraint_epsilon = constraints.get("normalization_epsilon")
    if (
        type(min_size) is not int
        or type(max_size) is not int
        or type(max_pixels) is not int
        or type(stride) is not int
        or min_size < 32
        or max_size < min_size
        or max_size > 1024
        or max_pixels < min_size * min_size
        or max_pixels > 262144
        or stride < 1
        or stride > max_size
        or not _is_positive_finite(constraint_epsilon)
    ):
        raise PasteVolumeModelPackageError("画像制約がv1安全範囲を満たしません")
    if float(epsilon) != float(constraint_epsilon):
        raise PasteVolumeModelPackageError("normalization epsilonが不一致です")
    if manifest.get("preprocess_schema_sha256") != _canonical_json_sha256(preprocess):
        raise PasteVolumeModelPackageError(
            "preprocess schema fingerprintが一致しません"
        )


def _validate_input_contract(manifest: Mapping[str, Any]) -> None:
    contract = _required_mapping(manifest, "input_contract")
    if (
        contract.get("batch_size") != 1
        or contract.get("dynamic_height_width") is not True
    ):
        raise PasteVolumeModelPackageError("batch 1、dynamic H/W contractが必要です")
    inputs = _required_mapping(contract, "inputs")
    outputs = _required_mapping(contract, "outputs")
    for name in ("image_6ch", "valid_pixel_mask", "pixel_per_mm"):
        _required_string(inputs, name)
    for name in ("mean_volume_ul", "log_variance_volume_ul2"):
        _required_string(outputs, name)


def _validate_training_coverage(manifest: Mapping[str, Any]) -> None:
    coverage = _required_mapping(manifest, "training_coverage")
    if set(coverage) != {"pixel_per_mm", "height", "width"}:
        raise PasteVolumeModelPackageError("training coverage key集合が不正です")
    for name in ("pixel_per_mm", "height", "width"):
        bounds = _required_mapping(coverage, name)
        if set(bounds) != {"min", "max"}:
            raise PasteVolumeModelPackageError(f"training coverage {name}が不正です")
        minimum = bounds.get("min")
        maximum = bounds.get("max")
        if (
            not _is_positive_finite(minimum)
            or not _is_positive_finite(maximum)
            or float(minimum) > float(maximum)
        ):
            raise PasteVolumeModelPackageError(f"training coverage {name}が不正です")


def _validate_evaluation_numbers(phase: Mapping[str, Any], *, phase_name: str) -> None:
    required_numbers = (
        "primary_accuracy_score",
        "coverage_68",
        "fp32_reference_accuracy_score",
        "fp32_reference_coverage_68",
        "maximum_mean_parity_error_ul",
        "maximum_mean_parity_tolerance_ul",
        "maximum_mean_parity_ratio",
        "maximum_log_variance_parity_error",
        "maximum_log_variance_parity_tolerance",
        "maximum_log_variance_parity_ratio",
        "padding_accuracy_degradation",
        "uncertainty_relative_std_threshold",
        "accepted_accuracy_score",
        "accepted_sample_coverage",
        "three_sample_acceptance",
    )
    if any(not _is_nonnegative_finite(phase.get(name)) for name in required_numbers):
        raise PasteVolumeModelPackageError(f"{phase_name} evaluation数値が不正です")
    if int(phase.get("sample_count", 0)) < 1:
        raise PasteVolumeModelPackageError(f"{phase_name} sample_countが不正です")
    if float(phase["primary_accuracy_score"]) > 0.10:
        raise PasteVolumeModelPackageError(
            f"{phase_name} primary accuracy gate失敗です"
        )
    if float(phase["padding_accuracy_degradation"]) > 0.01:
        raise PasteVolumeModelPackageError(f"{phase_name} padding gate失敗です")
    if float(phase["accepted_accuracy_score"]) > 0.10:
        raise PasteVolumeModelPackageError(
            f"{phase_name} accepted accuracy gate失敗です"
        )
    if not 0.0 < float(phase["accepted_sample_coverage"]) <= 1.0:
        raise PasteVolumeModelPackageError(f"{phase_name} accepted coverageが不正です")
    model_format = phase.get("model_format")
    if model_format == "onnx-fp32":
        if (
            phase.get("fp32_parity_passed") is not True
            or float(phase["maximum_mean_parity_ratio"]) > 1.0
            or float(phase["maximum_log_variance_parity_ratio"]) > 1.0
        ):
            raise PasteVolumeModelPackageError(f"{phase_name} FP32 parity gate失敗です")
    elif model_format == "onnx-int8-qdq":
        if (
            float(phase["primary_accuracy_score"])
            - float(phase["fp32_reference_accuracy_score"])
            > 0.01
        ):
            raise PasteVolumeModelPackageError(
                f"{phase_name} INT8 accuracy gate失敗です"
            )
        if (
            abs(
                float(phase["coverage_68"]) - float(phase["fp32_reference_coverage_68"])
            )
            > 0.03
        ):
            raise PasteVolumeModelPackageError(
                f"{phase_name} INT8 coverage gate失敗です"
            )
    else:
        raise PasteVolumeModelPackageError(f"{phase_name} model formatが不正です")
    if phase_name == "frozen_test" and float(phase["three_sample_acceptance"]) < 0.90:
        raise PasteVolumeModelPackageError(
            "frozen test 3-sample acceptance gate失敗です"
        )


def _preprocess_inputs(
    pre_rgb: ImageArray,
    post_rgb: ImageArray,
    *,
    pixel_per_mm: float,
    config: Mapping[str, Any],
) -> tuple[
    np.ndarray[Any, np.dtype[np.float32]],
    np.ndarray[Any, np.dtype[np.bool_]],
    np.ndarray[Any, np.dtype[np.float32]],
]:
    try:
        from .data import ImageConstraints, preprocess_rgb_pair
    except ImportError as exc:  # pragma: no cover - dependency error only
        raise PasteVolumeRuntimeError(
            "画像前処理にはml-runtime dependency groupが必要です"
        ) from exc

    raw_constraints = _required_mapping(config, "image_constraints")
    constraints = ImageConstraints(
        min_size=int(raw_constraints["min_size"]),
        max_size=int(raw_constraints["max_size"]),
        max_pixels=int(raw_constraints["max_pixels"]),
        stride=int(raw_constraints["stride"]),
        normalization_epsilon=float(raw_constraints["normalization_epsilon"]),
    )
    processed = preprocess_rgb_pair(
        pre_rgb,
        post_rgb,
        pixel_per_mm,
        constraints=constraints,
    )
    image = processed.image_6ch.detach().cpu().float().numpy()[None, ...]
    mask = processed.valid_pixel_mask.detach().cpu().numpy()[None, ...].astype(np.bool_)
    scale = np.asarray([[processed.pixel_per_mm]], dtype=np.float32)
    return image, mask, scale


def _coverage_rejection_reason(
    manifest: Mapping[str, Any],
    *,
    original_height: int,
    original_width: int,
    pixel_per_mm: float,
) -> str | None:
    coverage = _required_mapping(manifest, "training_coverage")
    checks = (
        ("pixel_per_mm", pixel_per_mm),
        ("height", float(original_height)),
        ("width", float(original_width)),
    )
    for name, value in checks:
        bounds = _required_mapping(coverage, name)
        minimum = bounds.get("min")
        maximum = bounds.get("max")
        if not _is_positive_finite(minimum) or not _is_positive_finite(maximum):
            raise PasteVolumeModelPackageError(f"training coverage {name}が不正です")
        if value < float(minimum) or value > float(maximum):
            return f"{name}={value:.6g}がtraining coverage外です"
    return None


def _validate_rgb_pair(
    pre_rgb: ImageArray, post_rgb: ImageArray, *, pixel_per_mm: float
) -> None:
    for name, image in (("pre_rgb", pre_rgb), ("post_rgb", post_rgb)):
        if not isinstance(image, np.ndarray):
            raise TypeError(f"{name}はnumpy.ndarrayが必要です")
        if image.dtype != np.uint8:
            raise ValueError(f"{name}はuint8 RGB画像が必要です")
        if image.ndim != 3 or image.shape[2] != 3:
            raise ValueError(f"{name}はHWC 3-channel RGB画像が必要です")
    if pre_rgb.shape != post_rgb.shape:
        raise ValueError("pre_rgbとpost_rgbは同じshapeが必要です")
    if not _is_positive_finite(pixel_per_mm):
        raise ValueError("pixel_per_mmは正の有限値が必要です")


def _single_output(value: object) -> float | None:
    array = np.asarray(value)
    if array.size != 1:
        return None
    item = array.reshape(-1)[0]
    try:
        return float(item)
    except (TypeError, ValueError):
        return None


def _finite_or_none(value: float) -> float | None:
    return value if math.isfinite(value) else None


def _rejected_prediction(
    model_id: str, reason: str, *, mean: float = math.nan
) -> PasteVolumePrediction:
    return PasteVolumePrediction(
        mean_volume_ul=mean,
        std_volume_ul=math.nan,
        relative_std=math.nan,
        accepted=False,
        rejection_reason=reason,
        model_id=model_id,
    )


def _read_sha256sums(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PasteVolumeModelPackageError(f"SHA256SUMSを読めません: {exc}") from exc
    for line in lines:
        parts = line.split("  ")
        if len(parts) != 2 or not _SHA256_PATTERN.fullmatch(parts[0]):
            raise PasteVolumeModelPackageError("SHA256SUMSの形式が不正です")
        filename = parts[1]
        if filename not in _PACKAGE_FILES or filename in result:
            raise PasteVolumeModelPackageError("SHA256SUMSのfile名が不正または重複です")
        result[filename] = parts[0]
    return result


def _read_json_object(path: Path, *, description: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PasteVolumeModelPackageError(f"{description}を読めません: {exc}") from exc
    if not isinstance(value, dict):
        raise PasteVolumeModelPackageError(f"{description}はJSON objectが必要です")
    return cast(Mapping[str, Any], value)


def _required_mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    item = value.get(key)
    if not isinstance(item, dict):
        raise PasteVolumeModelPackageError(f"{key}はobjectが必要です")
    return cast(Mapping[str, Any], item)


def _required_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise PasteVolumeModelPackageError(f"{key}は空でない文字列が必要です")
    return item


def _required_string_array(value: Mapping[str, Any], key: str) -> tuple[str, ...]:
    item = value.get(key)
    if (
        not isinstance(item, list)
        or not item
        or not all(isinstance(element, str) and element for element in item)
    ):
        raise PasteVolumeModelPackageError(f"{key}は空でない文字列arrayが必要です")
    if len(set(item)) != len(item):
        raise PasteVolumeModelPackageError(f"{key}が重複しています")
    return tuple(item)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
    except OSError as exc:
        raise PasteVolumeModelPackageError(
            f"fileをhash化できません: {path}: {exc}"
        ) from exc
    return digest.hexdigest()


def _canonical_json_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _version_tuple(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", value)
    return tuple(int(number) for number in numbers[:3])


def _is_positive_finite(value: object) -> TypeGuard[int | float]:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value > 0
    )


def _is_finite_number(value: object) -> TypeGuard[int | float]:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _is_nonnegative_finite(value: object) -> TypeGuard[int | float]:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
    )


def _is_prefixed_sha256(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and bool(_PREFIXED_SHA256_PATTERN.fullmatch(value))


def _is_sha256(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and bool(_SHA256_PATTERN.fullmatch(value))
