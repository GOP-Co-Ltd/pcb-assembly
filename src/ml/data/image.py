"""画像 sample を model 入力へそろえる前処理.

学習・評価・実運転推論で同じ順序を通す。

1. RGB の ``uint8 [3, H, W]`` として decode する
2. サイズ制約を満たす等方 scale と、必要なら回転を同じ parameter で適用する
3. ``[0, 1]`` の float32 へ変換し、channel 方向に連結する
4. その sample だけの平均と分散で全 channel を標準化する（SampleLayerNorm）

dataset 全体・batch・channel 別の統計は一切使わない。したがって前処理に
train dataset 由来の値が残らず、推論時も同じ関数で再現できる。
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Sequence
from pathlib import Path

import attrs
import torch
from torch import Tensor
from torchvision.io import ImageReadMode, decode_image
from torchvision.transforms import InterpolationMode
from torchvision.transforms.v2 import functional as transforms

_MINIMUM_VARIANCE = 1e-12
_FULL_TURN_DEGREES = 360.0


@attrs.frozen
class ImageShape:
    """画像の高さと幅."""

    height: int
    width: int

    @property
    def pixels(self) -> int:
        return self.height * self.width


@attrs.frozen
class ImageConstraints:
    """V1 encoder へ入れる前処理済み画像のサイズ制約.

    dataset 形式そのものの制限ではなく、計算量と入力品質のための制約。
    """

    minimum_size: int = 32
    maximum_size: int = 1024
    maximum_pixels: int = 262_144
    stride: int = 32

    def validate(self) -> str | None:
        """サイズ制約の整合を検証する."""

        if self.minimum_size < 1:
            return f"minimum_size は正の整数が必要です: {self.minimum_size}"
        if self.maximum_size < self.minimum_size:
            return f"maximum_size は minimum_size 以上が必要です: {self.maximum_size}"
        if self.maximum_pixels < self.minimum_size**2:
            return f"maximum_pixels が小さすぎます: {self.maximum_pixels}"
        if self.stride < 1:
            return f"stride は正の整数が必要です: {self.stride}"
        return None


@attrs.frozen
class AugmentationRange:
    """幾何 augmentation の探索範囲.

    輝度・contrast・色・blur・noise は変更しない。
    """

    rotation_enabled: bool = True
    minimum_scale: float = 0.8
    maximum_scale: float = 1.2

    def validate(self) -> str | None:
        """Augmentation 範囲の整合を検証する."""

        if not math.isfinite(self.minimum_scale) or self.minimum_scale <= 0:
            return f"minimum_scale は正の有限値が必要です: {self.minimum_scale}"
        if self.maximum_scale < self.minimum_scale:
            return (
                f"maximum_scale は minimum_scale 以上が必要です: {self.maximum_scale}"
            )
        return None


@attrs.frozen
class AugmentationParameters:
    """1 sample へ実際に適用する幾何変換."""

    rotation_degrees: float
    scale: float


NO_AUGMENTATION = AugmentationParameters(rotation_degrees=0.0, scale=1.0)


@attrs.frozen(eq=False)
class PreprocessedSample:
    """Model へ渡せる状態までそろえた 1 sample.

    Tensor は要素ごとの比較になり真偽値へ落ちないので、等価性は identity で決める。
    """

    image: Tensor
    valid_mask: Tensor
    scale: float


def augmentation_parameters(
    *,
    sample_id: str,
    global_seed: int,
    epoch: int,
    augmentation: AugmentationRange,
) -> AugmentationParameters:
    """``(global_seed, epoch, sample_id)`` から決定論的に変換を決める.

    大域的な乱数状態には依存させない。

    worker 数や中断再開で同じ sample の変換が変わらないようにするため。
    """

    unit_scale = augmentation.minimum_scale == 1.0 and augmentation.maximum_scale == 1.0
    if not augmentation.rotation_enabled and unit_scale:
        return NO_AUGMENTATION
    generator = random.Random(_derived_seed(f"{global_seed}:{epoch}:{sample_id}"))
    rotation = (
        generator.random() * _FULL_TURN_DEGREES
        if augmentation.rotation_enabled
        else 0.0
    )
    minimum = math.log(augmentation.minimum_scale)
    maximum = math.log(augmentation.maximum_scale)
    scale = math.exp(minimum + generator.random() * (maximum - minimum))
    return AugmentationParameters(rotation_degrees=rotation, scale=scale)


def _constraint_scale(shape: ImageShape, *, constraints: ImageConstraints) -> float:
    """最大辺と最大画素数の両方を満たす最大 scale を返す."""

    by_side = constraints.maximum_size / max(shape.height, shape.width)
    by_pixels = math.sqrt(constraints.maximum_pixels / shape.pixels)
    return min(by_side, by_pixels)


def _applied_scale(
    shape: ImageShape,
    *,
    constraints: ImageConstraints,
    parameters: AugmentationParameters,
) -> float:
    """元画像へ実際に掛ける等方 scale を返す.

    元画像を無条件に拡大はしない。augmentation の拡大はサイズ制約でクリップする。
    """

    limit = _constraint_scale(shape, constraints=constraints)
    return min(limit, min(1.0, limit) * parameters.scale)


def preprocessed_shape(
    shape: ImageShape,
    *,
    constraints: ImageConstraints,
    parameters: AugmentationParameters,
) -> ImageShape:
    """前処理後の高さ・幅を返す.

    極端に細長い画像を正方形へ歪めず、等方 scale だけで縮める。
    """

    scale = _applied_scale(shape, constraints=constraints, parameters=parameters)
    return ImageShape(
        height=max(1, math.floor(shape.height * scale)),
        width=max(1, math.floor(shape.width * scale)),
    )


def decode_rgb_image(path: Path) -> Tensor:
    """Lossless 画像を RGB 順の ``uint8 [3, H, W]`` として読む.

    OpenCV 由来の BGR 変換や HWC からの ``permute`` を挟まないための唯一の入口。
    """

    return decode_image(str(Path(path)), mode=ImageReadMode.RGB)


def sample_layer_norm(
    image: Tensor, *, valid_mask: Tensor | None = None, eps: float = 1e-5
) -> tuple[Tensor | None, str | None]:
    """Sample 1 個の全軸で標準化する（SampleLayerNorm）.

    全画素が有効なとき
    ``torch.nn.functional.layer_norm(image, image.shape, None, None, eps)``
    と同じ値になる。invalid 領域がある場合だけ、同じ計算を有効画素へ限定する。
    """

    if image.ndim != 3:
        raise ValueError(f"image は CHW tensor が必要です: {tuple(image.shape)}")
    values = image.to(torch.float32)
    valid = _resolved_valid_mask(values, valid_mask)
    expanded = valid.expand_as(values)
    count = int(expanded.sum().item())
    if count == 0:
        return None, "有効画素が 1 つもありません"
    # 乗算で mask すると invalid 側の NaN が 0 にならず伝播するため where を使う
    zero = torch.zeros((), dtype=values.dtype, device=values.device)
    visible = torch.where(expanded, values, zero)
    mean = visible.sum() / count
    variance = torch.where(expanded, torch.square(values - mean), zero).sum() / count
    if not bool(torch.isfinite(mean)) or not bool(torch.isfinite(variance)):
        return None, "平均または分散が有限値になりません"
    if float(variance.item()) < _MINIMUM_VARIANCE:
        return None, f"分散が小さすぎます: {float(variance.item()):.3e}"
    normalized = (values - mean) / torch.sqrt(variance + eps)
    return torch.where(expanded, normalized, zero), None


def preprocess_image_stack(
    images: Sequence[Tensor],
    *,
    constraints: ImageConstraints,
    parameters: AugmentationParameters,
    eps: float = 1e-5,
) -> tuple[PreprocessedSample | None, str | None]:
    """同じ大きさの RGB 画像列を 1 個の標準化済み tensor へまとめる."""

    if error := _validate_image_stack(images):
        return None, error
    original = ImageShape(int(images[0].shape[1]), int(images[0].shape[2]))
    target = preprocessed_shape(
        original, constraints=constraints, parameters=parameters
    )
    if min(target.height, target.width) < constraints.minimum_size:
        return None, (
            "前処理後の画像が minimum_size を下回ります: "
            f"{target.height}x{target.width} < {constraints.minimum_size}"
        )

    resized = [
        transforms.resize(
            image,
            [target.height, target.width],
            interpolation=InterpolationMode.BILINEAR,
            antialias=True,
        )
        for image in images
    ]
    valid = torch.ones((1, target.height, target.width), dtype=torch.uint8)
    if parameters.rotation_degrees:
        resized = [
            transforms.rotate(
                image,
                parameters.rotation_degrees,
                interpolation=InterpolationMode.BILINEAR,
            )
            for image in resized
        ]
        # rotate は tensor 入力で nearest-exact を受け付けない。mask は resize 後の
        # 全 true から作るので、補間が問題になるのは回転だけであり nearest で足りる。
        valid = transforms.rotate(
            valid,
            parameters.rotation_degrees,
            interpolation=InterpolationMode.NEAREST,
        )
    valid_mask = valid > 0

    stacked = torch.cat(
        [transforms.to_dtype(image, torch.float32, scale=True) for image in resized],
        dim=0,
    )
    normalized, error = sample_layer_norm(stacked, valid_mask=valid_mask, eps=eps)
    if normalized is None:
        return None, error
    return (
        PreprocessedSample(
            image=normalized,
            valid_mask=valid_mask,
            scale=_applied_scale(
                original, constraints=constraints, parameters=parameters
            ),
        ),
        None,
    )


def _validate_image_stack(images: Sequence[Tensor]) -> str | None:
    if not images:
        return "画像が 1 枚以上必要です"
    expected = images[0].shape
    for image in images:
        if image.ndim != 3 or int(image.shape[0]) != 3:
            return f"画像は RGB の CHW tensor が必要です: {tuple(image.shape)}"
        if image.dtype != torch.uint8:
            return f"画像は uint8 が必要です: {image.dtype}"
        if image.shape != expected:
            return (
                "画像は同じ高さ・幅が必要です: "
                f"{tuple(expected)} と {tuple(image.shape)}"
            )
    return None


def _resolved_valid_mask(values: Tensor, valid_mask: Tensor | None) -> Tensor:
    if valid_mask is None:
        return torch.ones(
            (1, *values.shape[1:]), dtype=torch.bool, device=values.device
        )
    if valid_mask.dtype != torch.bool or tuple(valid_mask.shape) != (
        1,
        *values.shape[1:],
    ):
        raise ValueError(
            f"valid mask は bool [1, H, W] が必要です: {tuple(valid_mask.shape)}"
        )
    return valid_mask


def _derived_seed(material: str) -> int:
    return int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest()[:8], "big")


__all__ = [
    "NO_AUGMENTATION",
    "AugmentationParameters",
    "AugmentationRange",
    "ImageConstraints",
    "ImageShape",
    "PreprocessedSample",
    "augmentation_parameters",
    "decode_rgb_image",
    "preprocess_image_stack",
    "preprocessed_shape",
    "sample_layer_norm",
]
