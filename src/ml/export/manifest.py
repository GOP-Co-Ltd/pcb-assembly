"""推論成果物の manifest.

Package の中身を「何の model か」「どう呼ぶか」まで含めて 1 ファイルに記録する。

推論側は package だけを受け取り、学習側の設定を持たずに読み込めるようにする。

入出力の軸は固定値も symbol も文字列で持つ。

数値と symbol の union にすると、JSON 表現が 2 通りになって突き合わせにくい。

この module は onnx も onnxruntime も import しない。

manifest は成果物の説明であって、読むために推論 runtime を要求しないため。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Literal

import attrs
from cattrs import Converter

from ml.artifact.document import DocumentKind
from ml.artifact.package import CHECKSUM_FILENAME
from ml.serialization import make_strict_converter

INFERENCE_MANIFEST_DOCUMENT = DocumentKind(
    kind="ml-inference-manifest", schema_version=1
)
MANIFEST_FILENAME = "manifest.json"
DEFAULT_MODEL_FILENAME = "model.onnx"

type Precision = Literal["float32", "static-int8"]

PRECISIONS: tuple[Precision, ...] = ("float32", "static-int8")
QUANTIZED_PRECISION: Precision = "static-int8"

_VERSION_PATTERN = re.compile(r"\d+(?:\.\d+)*")
_RESERVED_FILENAMES = frozenset({MANIFEST_FILENAME, CHECKSUM_FILENAME})
_CONVERTER = make_strict_converter()


def _read_only_mapping(values: Mapping[str, str]) -> Mapping[str, str]:
    """呼び出し側の辞書と切り離した、変更できない写像を返す."""

    return MappingProxyType(dict(values))


@attrs.frozen
class TensorContract:
    """Model の入出力 1 本ぶんの名前・要素型・軸.

    軸は ``("1", "6", "height", "width")`` のように文字列で並べる。

    10 進数字だけの軸は固定長、識別子の軸は dynamic な symbol を表す。
    """

    name: str
    element_type: str
    dimensions: tuple[str, ...]

    def validate(self) -> str | None:
        """名前・要素型・軸の綴りを検証する."""

        if not self.name:
            return "TensorContract の name は空にできません"
        if not self.element_type:
            return f"element_type は空にできません: {self.name}"
        if not self.dimensions:
            return f"dimensions は 1 軸以上が必要です: {self.name}"
        for dimension in self.dimensions:
            if not dimension:
                return f"dimensions に空の軸があります: {self.name}"
            if not dimension.isdigit() and not dimension.isidentifier():
                return (
                    "軸は 10 進数か識別子が必要です: " f"{self.name} の {dimension!r}"
                )
        return None

    @property
    def dynamic_dimension_names(self) -> tuple[str, ...]:
        """固定値でない軸の symbol を、現れた順に重複なく返す."""

        names: list[str] = []
        for dimension in self.dimensions:
            if not dimension.isdigit() and dimension not in names:
                names.append(dimension)
        return tuple(names)


@attrs.frozen
class QuantizationRecord:
    """Static 量子化の設定と、校正に使った sample の同一性.

    校正 sample の id を残すのは、事後に split の取り違えを検出するため。
    """

    method: str
    activation_type: str
    weight_type: str
    per_channel: bool
    quantized_operator_types: tuple[str, ...]
    calibration_split: str
    calibration_sample_ids: tuple[str, ...]

    def validate(self) -> str | None:
        """量子化設定と校正 sample の記録が揃っているかを検証する."""

        for name in ("method", "activation_type", "weight_type", "calibration_split"):
            value: str = getattr(self, name)
            if not value:
                return f"{name} は空にできません"
        if not self.quantized_operator_types:
            return "quantized_operator_types は 1 個以上が必要です"
        if any(not item for item in self.quantized_operator_types):
            return "quantized_operator_types に空の operator 名があります"
        if len(set(self.quantized_operator_types)) != len(
            self.quantized_operator_types
        ):
            return (
                "quantized_operator_types が重複しています: "
                f"{sorted(self.quantized_operator_types)}"
            )
        if not self.calibration_sample_ids:
            return "calibration_sample_ids は 1 個以上が必要です"
        if any(not item for item in self.calibration_sample_ids):
            return "calibration_sample_ids に空の id があります"
        if len(set(self.calibration_sample_ids)) != len(self.calibration_sample_ids):
            return "calibration_sample_ids が重複しています"
        return None


@attrs.frozen
class InferenceManifest:
    """Package 1 個ぶんの推論契約.

    ``extra_payload_filenames`` にはドメイン側の付随 JSON が入る。

    ``ml`` はその中身を解釈せず、package の構成要素としてだけ扱う。
    """

    model_filename: str
    precision: Precision
    opset_version: int
    onnx_ir_version: int
    inputs: tuple[TensorContract, ...]
    outputs: tuple[TensorContract, ...]
    minimum_onnxruntime_version: str
    exporter_versions: Mapping[str, str] = attrs.field(converter=_read_only_mapping)
    training_run_id: str
    dataset_fingerprint: str
    split_fingerprint: str
    quantization: QuantizationRecord | None = None
    extra_payload_filenames: tuple[str, ...] = ()

    def validate(self) -> str | None:
        """Manifest 単体で閉じる整合をすべて検証する."""

        if error := _validate_payload_filename(self.model_filename, "model_filename"):
            return error
        if self.precision not in PRECISIONS:
            return f"未知の precision です: {self.precision!r}"
        if self.opset_version < 1:
            return f"opset_version は正の整数が必要です: {self.opset_version}"
        if self.onnx_ir_version < 1:
            return f"onnx_ir_version は正の整数が必要です: {self.onnx_ir_version}"
        if error := _validate_contracts(self.inputs, "inputs"):
            return error
        if error := _validate_contracts(self.outputs, "outputs"):
            return error
        if not _VERSION_PATTERN.fullmatch(self.minimum_onnxruntime_version):
            return (
                "minimum_onnxruntime_version は数字と '.' だけが必要です: "
                f"{self.minimum_onnxruntime_version!r}"
            )
        if not self.exporter_versions:
            return "exporter_versions は 1 件以上が必要です"
        if any(not key or not value for key, value in self.exporter_versions.items()):
            return "exporter_versions に空の名前か版数があります"
        for name in ("training_run_id", "dataset_fingerprint", "split_fingerprint"):
            value: str = getattr(self, name)
            if not value:
                return f"{name} は空にできません"
        if error := _validate_extra_filenames(
            self.extra_payload_filenames, self.model_filename
        ):
            return error
        if error := _validate_quantization(self.precision, self.quantization):
            return error
        return None

    def payload_filenames(self) -> tuple[str, ...]:
        """Package が持つべき payload 名を整列して返す.

        ``ImmutablePackage.publish`` の ``payload_filenames`` へそのまま渡せる。

        重複除去はしない。:meth:`validate` を通った manifest では
        manifest 名・model 名・extra が互いに相異なることが保証されるため。
        """

        return tuple(
            sorted(
                (MANIFEST_FILENAME, self.model_filename, *self.extra_payload_filenames)
            )
        )

    def verify_runtime(self, *, onnxruntime_version: str) -> str | None:
        """実行環境の onnxruntime が要求版を満たすかを検証する.

        比較するのは先頭の dotted 数値だけで、``+cpu`` などの後置きは無視する。

        ``1.29`` と ``1.29.0`` は同じ版として扱う。
        """

        required = _version_numbers(self.minimum_onnxruntime_version)
        if required is None:
            return (
                "minimum_onnxruntime_version を解釈できません: "
                f"{self.minimum_onnxruntime_version!r}"
            )
        actual = _version_numbers(onnxruntime_version)
        if actual is None:
            return f"onnxruntime の版を解釈できません: {onnxruntime_version!r}"
        width = max(len(required), len(actual))
        if _padded(actual, width) < _padded(required, width):
            return (
                "onnxruntime "
                f"{self.minimum_onnxruntime_version} 以上が必要です: "
                f"{onnxruntime_version}"
            )
        return None

    def save(self, path: Path) -> None:
        """Manifest を envelope 付き JSON として atomic に書き出す."""

        INFERENCE_MANIFEST_DOCUMENT.save(path, self, converter=_CONVERTER)

    @classmethod
    def load(cls, path: Path) -> tuple[InferenceManifest | None, str | None]:
        """Manifest を読み、構造か整合が崩れていれば理由を返す."""

        manifest, error = INFERENCE_MANIFEST_DOCUMENT.load(
            path, cls, converter=_CONVERTER
        )
        if manifest is None:
            return None, error
        if error := manifest.validate():
            return None, f"manifest の内容が不正です: {error}"
        return manifest, None


def _validate_contracts(
    contracts: tuple[TensorContract, ...], label: str
) -> str | None:
    if not contracts:
        return f"{label} は 1 本以上が必要です"
    for contract in contracts:
        if error := contract.validate():
            return f"{label}: {error}"
    names = [contract.name for contract in contracts]
    if len(set(names)) != len(names):
        return f"{label} の名前が重複しています: {sorted(names)}"
    return None


def _validate_quantization(
    precision: Precision, quantization: QuantizationRecord | None
) -> str | None:
    """Precision と量子化記録の組み合わせが実在し得るかを検証する.

    Manifest は Pi へ出荷する唯一の記述なので、内部で矛盾する組み合わせを弾く。
    """

    if precision == QUANTIZED_PRECISION and quantization is None:
        return (
            f"precision が {QUANTIZED_PRECISION} の manifest には "
            "quantization が必要です"
        )
    if precision != QUANTIZED_PRECISION and quantization is not None:
        return (
            "quantization を持てるのは precision が "
            f"{QUANTIZED_PRECISION} の manifest だけです: {precision!r}"
        )
    if quantization is not None and (error := quantization.validate()):
        return f"quantization: {error}"
    return None


def _validate_payload_filename(filename: str, label: str) -> str | None:
    if not filename:
        return f"{label} は空にできません"
    if Path(filename).name != filename:
        return f"{label} はディレクトリを含まない名前が必要です: {filename!r}"
    if filename in _RESERVED_FILENAMES:
        return f"{label} に予約名は使えません: {filename!r}"
    return None


def _validate_extra_filenames(
    filenames: tuple[str, ...], model_filename: str
) -> str | None:
    for filename in filenames:
        if error := _validate_payload_filename(filename, "extra_payload_filenames"):
            return error
        if filename == model_filename:
            return (
                "extra_payload_filenames に model_filename が重複しています: "
                f"{filename!r}"
            )
    if len(set(filenames)) != len(filenames):
        return f"extra_payload_filenames が重複しています: {sorted(filenames)}"
    return None


def _version_numbers(version: str) -> tuple[int, ...] | None:
    matched = _VERSION_PATTERN.match(version)
    if matched is None:
        return None
    return tuple(int(part) for part in matched.group().split("."))


def _padded(numbers: tuple[int, ...], width: int) -> tuple[int, ...]:
    return numbers + (0,) * (width - len(numbers))


__all__ = [
    "DEFAULT_MODEL_FILENAME",
    "INFERENCE_MANIFEST_DOCUMENT",
    "MANIFEST_FILENAME",
    "PRECISIONS",
    "QUANTIZED_PRECISION",
    "InferenceManifest",
    "Precision",
    "QuantizationRecord",
    "TensorContract",
]
