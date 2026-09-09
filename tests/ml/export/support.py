"""``ml.export`` のテストが共有する tiny model と ONNX 生成ヘルパー.

6 channel 画像と条件変数を受け取る小さな Gaussian 回帰 model を組み、その FP32 ONNX を
ファイルへ書き出す。

実 ONNX と実 ONNX Runtime を相手にするので、規模は 32x32 画像・16 channel まで
落としてある。

Encoder の深さは意図的に 2 stage 4 block としている。GroupNorm の初期値が同一の
initializer へ重複排除され、``op_types_to_quantize`` を絞らない量子化が実際に失敗する
構成だから。

装置ドメイン (``pcbasm``) も ``tests.helpers`` も import しない。

``--doctest-modules`` で collect されるため、doctest として解釈される記法は書かない。
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping, Sequence
from functools import cache
from pathlib import Path
from typing import override

import attrs
import numpy as np
import onnxruntime
import torch
from numpy.typing import NDArray
from torch import Tensor, nn

from ml.artifact.package import ImmutablePackage
from ml.export.graph import OnnxGraphSummary
from ml.export.manifest import (
    DEFAULT_MODEL_FILENAME,
    MANIFEST_FILENAME,
    InferenceManifest,
    TensorContract,
)
from ml.model.blocks import ImageEncoder, ImageEncoderConfig
from ml.model.heads import (
    GaussianHeadConfig,
    GaussianImageRegressor,
    GaussianRegressionHead,
)

IMAGE_CHANNELS = 6
CONDITIONING_FEATURES = 2
IMAGE_SIZE = 32

IMAGES_INPUT = "images"
CONDITIONING_INPUT = "conditioning"
INPUT_NAMES = (IMAGES_INPUT, CONDITIONING_INPUT)

MEAN_OUTPUT = "predicted_mean"
LOG_VARIANCE_OUTPUT = "predicted_log_variance"
OUTPUT_NAMES = (MEAN_OUTPUT, LOG_VARIANCE_OUTPUT)

HEIGHT_SYMBOL = "height"
WIDTH_SYMBOL = "width"
BATCH_SYMBOL = "batch"

OPSET_VERSION = 20
ONNX_IR_VERSION = 10
MODEL_FILENAME = DEFAULT_MODEL_FILENAME

# 学習機と Raspberry Pi 5 のどちらでも満たせる下限。実際の版はこれより新しい
MINIMUM_ONNXRUNTIME_VERSION = "1.20"

# 出荷形。batch は 1 固定、高さと幅だけを dynamic にする
#
# ``torch.export`` は dict 形式の ``dynamic_shapes`` に全引数名を要求するので、
# dynamic 軸を持たない入力も空 dict で並べる。
DYNAMIC_SHAPES: Mapping[str, Mapping[int, str]] = {
    IMAGES_INPUT: {2: HEIGHT_SYMBOL, 3: WIDTH_SYMBOL},
    CONDITIONING_INPUT: {},
}

ENCODER_CONFIG = ImageEncoderConfig(
    input_channels=IMAGE_CHANNELS,
    stem_channels=(8,),
    stem_strides=(2,),
    stage_channels=(8, 16),
    stage_strides=(2, 2),
    blocks_per_stage=(2, 2),
    group_norm_groups=8,
)

HEAD_CONFIG = GaussianHeadConfig(
    input_features=ENCODER_CONFIG.output_features,
    conditioning_features=CONDITIONING_FEATURES,
    hidden_features=8,
)


class TinyRegressor(nn.Module):
    """画像と条件変数から平均と log 分散を返す tiny model.

    ``GaussianImageRegressor`` は mask も受け取る 3 引数だが、export の入口は位置引数を
    そのまま ONNX の入力にするので、出荷形と同じ 2 入力へ絞った薄い wrapper にする。
    """

    def __init__(self, *, seed: int = 0) -> None:
        super().__init__()
        torch.manual_seed(seed)
        self._regressor = GaussianImageRegressor(
            ImageEncoder(ENCODER_CONFIG), GaussianRegressionHead(HEAD_CONFIG)
        )

    @override
    def forward(self, images: Tensor, conditioning: Tensor) -> tuple[Tensor, Tensor]:
        """``[B, 6, H, W]`` と ``[B, 2]`` から平均と log 分散を返す."""

        return self._regressor(images, None, conditioning)


class SignedOutputsModel(nn.Module):
    """常に正の出力と常に負の出力を 1 本ずつ返す tiny model.

    ``positive_output_names`` の判定を、学習済みでない重みに依存せず決定的に確かめるため
    に使う。
    """

    @override
    def forward(self, values: Tensor) -> tuple[Tensor, Tensor]:
        """正の出力と、その符号を反転した出力を返す."""

        magnitude = torch.nn.functional.softplus(values) + 1.0
        return magnitude, -magnitude


class NonFiniteOutputModel(nn.Module):
    """非有限値だけを返す tiny model.

    0 除算なので eager でも ONNX でも同じ無限大になる。
    """

    @override
    def forward(self, values: Tensor) -> tuple[Tensor]:
        """入力を 0 で割った結果を 1 本返す."""

        return (values / torch.zeros_like(values),)


class DropoutModel(nn.Module):
    """Dropout を 1 段だけ持つ tiny model.

    学習 mode のまま export すると graph に Dropout node が残る。

    export の入口が ``eval()`` を強制していることを、graph の形から観測するために使う。
    """

    def __init__(self) -> None:
        super().__init__()
        torch.manual_seed(0)
        self._linear = nn.Linear(4, 2)
        self._dropout = nn.Dropout(p=0.5)

    @override
    def forward(self, values: Tensor) -> tuple[Tensor]:
        """線形変換に dropout を掛けた出力を 1 本返す."""

        return (self._dropout(self._linear(values)),)


class ConstantOffsetModel(nn.Module):
    """入力へ固定の下駄を履かせるだけの tiny model.

    parity が崩れた側を作るために使う。
    """

    def __init__(self, offset: float) -> None:
        super().__init__()
        self._offset = offset

    @override
    def forward(self, values: Tensor) -> tuple[Tensor, Tensor]:
        """下駄を履かせた正の出力と、その符号反転を返す."""

        magnitude = torch.nn.functional.softplus(values) + 1.0 + self._offset
        return magnitude, -magnitude


def build_tiny_model(*, seed: int = 0) -> TinyRegressor:
    """評価 mode の tiny model を決定論的に組む."""

    return TinyRegressor(seed=seed).eval()


def example_inputs(
    *,
    batch: int = 1,
    height: int = IMAGE_SIZE,
    width: int = IMAGE_SIZE,
    seed: int = 0,
) -> tuple[Tensor, Tensor]:
    """Tiny model の入力 tensor を決定論的に作る."""

    generator = torch.Generator().manual_seed(seed)
    images = torch.rand(
        (batch, IMAGE_CHANNELS, height, width),
        generator=generator,
        dtype=torch.float32,
    )
    conditioning = torch.rand(
        (batch, CONDITIONING_FEATURES), generator=generator, dtype=torch.float32
    )
    return images, conditioning


def input_values(
    *,
    batch: int = 1,
    height: int = IMAGE_SIZE,
    width: int = IMAGE_SIZE,
    seed: int = 0,
) -> dict[str, NDArray[np.float32]]:
    """ONNX 入力名をキーにした ndarray の組を作る."""

    images, conditioning = example_inputs(
        batch=batch, height=height, width=width, seed=seed
    )
    return {
        IMAGES_INPUT: images.numpy(),
        CONDITIONING_INPUT: conditioning.numpy(),
    }


def write_onnx_model(
    model_path: Path,
    *,
    model: nn.Module | None = None,
    example: Sequence[Tensor] | None = None,
    input_names: Sequence[str] = INPUT_NAMES,
    output_names: Sequence[str] = OUTPUT_NAMES,
    dynamic_shapes: Mapping[str, Mapping[int, str]] | None = None,
    opset_version: int = OPSET_VERSION,
) -> Path:
    """Tiny model の FP32 ONNX を指定 path へ書き出す.

    ``ml.export`` の実装を経由せずに書き出す。graph 検査そのもののテストが、検査対象の
    生成に同じ層を使わないようにするため。

    ``external_data=False`` を明示する。既定の ``True`` は重みを ``model.onnx.data`` へ
    切り出すので、manifest が 1 個の ``model_filename`` しか持たない出荷形と食い違う。
    """

    exported = build_tiny_model() if model is None else model
    arguments = tuple(example_inputs()) if example is None else tuple(example)
    shapes = DYNAMIC_SHAPES if dynamic_shapes is None else dynamic_shapes
    model_path.parent.mkdir(parents=True, exist_ok=True)
    with torch.no_grad():
        torch.onnx.export(
            exported,
            arguments,
            str(model_path),
            dynamo=True,
            verbose=False,
            external_data=False,
            opset_version=opset_version,
            input_names=list(input_names),
            output_names=list(output_names),
            dynamic_shapes=dict(shapes),
        )
    return model_path


@cache
def shared_fp32_model_path() -> Path:
    """出荷形の FP32 ONNX を 1 度だけ書き出し、その path を返す.

    ONNX export は 1 回あたり数秒かかる。

    読むだけのテストが毎回 export しないよう、process 内で 1 度だけ共有する。
    """

    directory = Path(tempfile.mkdtemp(prefix="ml-export-shared-"))
    return write_onnx_model(directory / MODEL_FILENAME)


def copy_shared_fp32_model(directory: Path) -> Path:
    """共有 FP32 ONNX を指定ディレクトリへ複製する."""

    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / MODEL_FILENAME
    destination.write_bytes(shared_fp32_model_path().read_bytes())
    return destination


def calibration_values(count: int) -> list[dict[str, NDArray[np.float32]]]:
    """量子化の calibration に使う入力を決定論的に作る."""

    return [input_values(seed=100 + index) for index in range(count)]


def model_tensor_contracts() -> (
    tuple[tuple[TensorContract, ...], tuple[TensorContract, ...]]
):
    """共有 FP32 ONNX から入出力の契約を読み出す.

    ``element_type`` の綴りを推測せず、実 graph が名乗る値をそのまま使う。
    """

    summary, error = OnnxGraphSummary.inspect(shared_fp32_model_path())
    assert summary is not None, error
    return (
        tuple(tensor.as_tensor_contract() for tensor in summary.inputs),
        tuple(tensor.as_tensor_contract() for tensor in summary.outputs),
    )


def build_manifest(**overrides: object) -> InferenceManifest:
    """共有 FP32 ONNX に対応する manifest を作る."""

    inputs, outputs = model_tensor_contracts()
    manifest = InferenceManifest(
        model_filename=MODEL_FILENAME,
        precision="float32",
        opset_version=OPSET_VERSION,
        onnx_ir_version=ONNX_IR_VERSION,
        inputs=inputs,
        outputs=outputs,
        minimum_onnxruntime_version=MINIMUM_ONNXRUNTIME_VERSION,
        exporter_versions={"torch": torch.__version__},
        training_run_id="run-0001",
        dataset_fingerprint="0123456789abcdef",
        split_fingerprint="fedcba9876543210",
    )
    return attrs.evolve(manifest, **overrides)


def publish_model_package(
    destination: Path,
    *,
    manifest: InferenceManifest | None = None,
    model_path: Path | None = None,
    extra_payloads: Mapping[str, bytes] | None = None,
) -> ImmutablePackage:
    """Manifest と ONNX を含む不変 package を publish する."""

    published = build_manifest() if manifest is None else manifest
    source = shared_fp32_model_path() if model_path is None else model_path
    extras = dict(extra_payloads or {})

    def write_payloads(directory: Path) -> None:
        (directory / published.model_filename).write_bytes(source.read_bytes())
        published.save(directory / MANIFEST_FILENAME)
        for filename, data in extras.items():
            (directory / filename).write_bytes(data)

    return ImmutablePackage.publish(
        destination,
        payload_filenames=sorted({*published.payload_filenames(), *extras}),
        write_payloads=write_payloads,
    )


def write_ort_optimized_model(model_path: Path, *, source: Path | None = None) -> Path:
    """ONNX Runtime が最適化した graph をファイルへ書き出す.

    独自 domain (``com.microsoft`` / ``com.microsoft.nchwc``) の node を含む実物を作る。

    この形を出荷 artifact にしないという計画 §2.6 の判断を、検査側から確かめるために使う。
    """

    options = onnxruntime.SessionOptions()
    options.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
    model_path.parent.mkdir(parents=True, exist_ok=True)
    options.optimized_model_filepath = str(model_path)
    onnxruntime.InferenceSession(
        str(shared_fp32_model_path() if source is None else source),
        options,
        providers=["CPUExecutionProvider"],
    )
    return model_path
