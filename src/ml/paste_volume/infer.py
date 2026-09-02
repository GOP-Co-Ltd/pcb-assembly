"""Paste-volume prediction API backed by a verified ONNX package."""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

import attrs
import numpy as np

from ml.infer.onnx import (
    OnnxRunError,
    OnnxSession,
    OnnxSessionError,
    OnnxSignature,
    OnnxTensorSpec,
)
from ml.infer.package import ImmutablePackageError, load_active_pointer
from pcbasm.vision import ImageArray

from .artifact import (
    is_positive_finite as _is_positive_finite,
    required_mapping as _required_mapping,
    required_string as _required_string,
)
from .package import (
    ACTIVE_MODEL_POINTER_SCHEMA_VERSION,
    PasteVolumeModelPackageError,
    VerifiedModelPackage,
    validate_embedded_release_metadata,
    verify_model_package,
)


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


class _OnnxPasteVolumeEstimator:
    def __init__(self, package: VerifiedModelPackage) -> None:
        runtime = _required_mapping(package.manifest, "runtime")
        minimum_version = _required_string(runtime, "minimum_version")
        try:
            self._session = OnnxSession.load_cpu(
                package.path / "model.onnx",
                minimum_version=minimum_version,
            )
        except OnnxSessionError as exc:
            raise PasteVolumeRuntimeError(
                f"ONNX Runtime sessionを初期化できません: {exc}"
            ) from exc

        self._package = package
        validate_embedded_release_metadata(
            package.manifest,
            self._session.metadata,
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
        try:
            self._session.require_signature(
                OnnxSignature(
                    inputs=(
                        OnnxTensorSpec(self._image_input, "tensor(float)"),
                        OnnxTensorSpec(self._mask_input, "tensor(bool)"),
                        OnnxTensorSpec(self._scale_input, "tensor(float)"),
                    ),
                    outputs=(
                        OnnxTensorSpec(self._mean_output, "tensor(float)"),
                        OnnxTensorSpec(
                            self._log_variance_output,
                            "tensor(float)",
                        ),
                    ),
                )
            )
        except OnnxSessionError as exc:
            raise PasteVolumeRuntimeError(
                f"ONNX contractがmanifestと一致しません: {exc}"
            ) from exc
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
        except OnnxRunError as exc:
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

    try:
        pointer = load_active_pointer(
            pointer_file,
            schema_version=ACTIVE_MODEL_POINTER_SCHEMA_VERSION,
        )
    except (ImmutablePackageError, OSError) as exc:
        message = str(exc)
        if "outside the immutable model package" in message:
            message = (
                "active model pointerはimmutable model packageの外に配置してください"
            )
        raise PasteVolumeModelPackageError(message) from exc
    active_path = pointer.active_package_path
    if not active_path.is_dir():
        raise PasteVolumeModelPackageError(
            f"active model packageが存在しません: {active_path}"
        )
    verified = verify_model_package(active_path, require_promoted=True)
    if verified.model_sha256 != pointer.active_package_sha256:
        raise PasteVolumeModelPackageError("active pointerのmodel checksumが不一致です")
    model = _required_mapping(verified.manifest, "model")
    if model.get("id") != pointer.active_package_id:
        raise PasteVolumeModelPackageError("active pointerのmodel idが不一致です")
    return active_path


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
