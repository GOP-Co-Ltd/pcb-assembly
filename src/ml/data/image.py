"""画像 sample を model 入力へそろえる前処理.

学習・評価・実運転推論で同じ順序を通す。

1. RGB の ``uint8 [3, H, W]`` として decode する
2. サイズ制約を満たす等方 scale と、必要なら回転を同じ parameter で適用する
3. ``[0, 1]`` の float32 へ変換し、channel 方向に連結する
4. その sample だけの平均と分散で全 channel を標準化する（SampleLayerNorm）

dataset 全体・batch・channel 別の統計は一切使わない。したがって前処理に
train dataset 由来の値が残らず、推論時も同じ関数で再現できる。

多視点 sample も同じ順序を通す。全 view へ同一の幾何変換を適用し、view を
またいだ 1 組の平均と分散で標準化するので、view 間の明るさ差が残る。
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

    def preprocessed(
        self,
        *,
        constraints: ImageConstraints,
        parameters: AugmentationParameters,
    ) -> ImageShape:
        """前処理後の高さ・幅を返す.

        極端に細長い画像を正方形へ歪めず、等方 scale だけで縮める。
        """

        scale = _applied_scale(self, constraints=constraints, parameters=parameters)
        return ImageShape(
            height=max(1, math.floor(self.height * scale)),
            width=max(1, math.floor(self.width * scale)),
        )


@attrs.frozen
class ImageConstraints:
    """V1 encoder へ入れる前処理済み画像のサイズ制約.

    dataset 形式そのものの制限ではなく、計算量と入力品質のための制約。

    既定値は点塗布 crop の実寸を通す値にそろえてある。
    """

    minimum_size: int = 16
    maximum_size: int = 512
    maximum_pixels: int = 262_144
    stride: int = 8

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

    def validate_augmentation(
        self, augmentation: AugmentationRange, *, smallest_source_size: int
    ) -> str | None:
        """源画像の最小辺と augmentation の scale 幅の組み合わせを検証する.

        前処理は ``minimum_size`` を下回った sample を捨てるので、設定の段階で
        弾かないと学習中に母集団が黙って減る。

        上限側は sample を落とさない代わりにサイズ制約で clip され、設定した
        振り幅がそのまま出ない。

        最小の源すら clip されるなら設定が誤っているので、同じく設定の段階で
        弾く。
        """

        if error := augmentation.validate():
            return error
        smallest = math.floor(smallest_source_size * augmentation.minimum_scale)
        if smallest < self.minimum_size:
            return (
                "最小 scale での前処理後サイズが minimum_size を下回ります: "
                f"{smallest} < {self.minimum_size}"
                f"（源 {smallest_source_size} px、"
                f"minimum_scale {augmentation.minimum_scale}）"
            )
        largest = math.floor(smallest_source_size * augmentation.maximum_scale)
        if largest > self.maximum_size:
            return (
                "最大 scale での前処理後サイズが maximum_size を超えます: "
                f"{largest} > {self.maximum_size}"
                f"（源 {smallest_source_size} px、"
                f"maximum_scale {augmentation.maximum_scale}）"
            )
        return None


@attrs.frozen
class AugmentationRange:
    """幾何 augmentation の探索範囲.

    輝度・contrast・色・blur・noise は変更しない。

    既定の scale 幅は、入力として受け付ける範囲より狭く取ってある。
    """

    rotation_enabled: bool = True
    minimum_scale: float = 0.5
    maximum_scale: float = 2.0

    def validate(self) -> str | None:
        """Augmentation 範囲の整合を検証する."""

        if not math.isfinite(self.minimum_scale) or self.minimum_scale <= 0:
            return f"minimum_scale は正の有限値が必要です: {self.minimum_scale}"
        if self.maximum_scale < self.minimum_scale:
            return (
                f"maximum_scale は minimum_scale 以上が必要です: {self.maximum_scale}"
            )
        return None

    def parameters_for(
        self, *, sample_id: str, global_seed: int, epoch: int
    ) -> AugmentationParameters:
        """``(global_seed, epoch, sample_id)`` から決定論的に変換を決める.

        大域的な乱数状態には依存させない。

        worker 数や中断再開で同じ sample の変換が変わらないようにするため。
        """

        unit_scale = self.minimum_scale == 1.0 and self.maximum_scale == 1.0
        if not self.rotation_enabled and unit_scale:
            return NO_AUGMENTATION
        generator = random.Random(_derived_seed(f"{global_seed}:{epoch}:{sample_id}"))
        rotation = (
            generator.random() * _FULL_TURN_DEGREES if self.rotation_enabled else 0.0
        )
        minimum = math.log(self.minimum_scale)
        maximum = math.log(self.maximum_scale)
        scale = math.exp(minimum + generator.random() * (maximum - minimum))
        return AugmentationParameters(rotation_degrees=rotation, scale=scale)


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

    @classmethod
    def preprocess(
        cls,
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
        target, error = _target_shape(
            original, constraints=constraints, parameters=parameters
        )
        if target is None:
            return None, error

        valid_mask = _valid_pixel_mask(target, parameters)
        stacked = _transformed_images(images, target=target, parameters=parameters)
        normalized, error = sample_layer_norm(stacked, valid_mask=valid_mask, eps=eps)
        if normalized is None:
            return None, error
        return (
            cls(
                image=normalized,
                valid_mask=valid_mask,
                scale=_applied_scale(
                    original, constraints=constraints, parameters=parameters
                ),
            ),
            None,
        )


@attrs.frozen(eq=False)
class PreprocessedMultiViewSample:
    """同一対象を複数の視点から撮った 1 sample.

    幾何 augmentation を全 view で共有するので、有効画素 mask は 1 枚で足りる。

    標準化も view をまたいだ 1 組の平均と分散で行い、view 間の明るさ差を保つ。

    Tensor は要素ごとの比較になり真偽値へ落ちないので、等価性は identity で決める。
    """

    images: Tensor
    valid_mask: Tensor
    scale: float

    @property
    def view_count(self) -> int:
        """保持している view の枚数."""

        return int(self.images.shape[0])

    @classmethod
    def preprocess(
        cls,
        views: Sequence[Sequence[Tensor]],
        *,
        constraints: ImageConstraints,
        parameters: AugmentationParameters,
        eps: float = 1e-5,
    ) -> tuple[PreprocessedMultiViewSample | None, str | None]:
        """View ごとの RGB 画像列を ``[V, C, H, W]`` の tensor へまとめる.

        ``views[v]`` は view ``v`` の画像列で、全 view・全画像が同じ高さ・幅で
        あることを要求する。
        """

        if error := _validate_view_stacks(views):
            return None, error
        leading = views[0][0]
        original = ImageShape(int(leading.shape[1]), int(leading.shape[2]))
        target, error = _target_shape(
            original, constraints=constraints, parameters=parameters
        )
        if target is None:
            return None, error

        valid_mask = _valid_pixel_mask(target, parameters)
        stacked = torch.stack(
            [
                _transformed_images(images, target=target, parameters=parameters)
                for images in views
            ]
        )
        view_count, channels = stacked.shape[0], stacked.shape[1]
        normalized, error = sample_layer_norm(
            stacked.flatten(0, 1), valid_mask=valid_mask, eps=eps
        )
        if normalized is None:
            return None, error
        return (
            cls(
                images=normalized.unflatten(0, (view_count, channels)),
                valid_mask=valid_mask,
                scale=_applied_scale(
                    original, constraints=constraints, parameters=parameters
                ),
            ),
            None,
        )


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


def _target_shape(
    original: ImageShape,
    *,
    constraints: ImageConstraints,
    parameters: AugmentationParameters,
) -> tuple[ImageShape | None, str | None]:
    """前処理後の形を求め、下限を割るなら理由を返す."""

    target = original.preprocessed(constraints=constraints, parameters=parameters)
    if min(target.height, target.width) < constraints.minimum_size:
        return None, (
            "前処理後の画像が minimum_size を下回ります: "
            f"{target.height}x{target.width} < {constraints.minimum_size}"
        )
    return target, None


def _transformed_images(
    images: Sequence[Tensor],
    *,
    target: ImageShape,
    parameters: AugmentationParameters,
) -> Tensor:
    """Resize と回転を掛け、channel 方向に連結した float32 を返す."""

    resized = [
        transforms.resize(
            image,
            [target.height, target.width],
            interpolation=InterpolationMode.BILINEAR,
            antialias=True,
        )
        for image in images
    ]
    if parameters.rotation_degrees:
        resized = [
            transforms.rotate(
                image,
                parameters.rotation_degrees,
                interpolation=InterpolationMode.BILINEAR,
            )
            for image in resized
        ]
    return torch.cat(
        [transforms.to_dtype(image, torch.float32, scale=True) for image in resized],
        dim=0,
    )


def _valid_pixel_mask(target: ImageShape, parameters: AugmentationParameters) -> Tensor:
    """回転で画像の外へ出た画素を除いた有効画素 mask を返す."""

    valid = torch.ones((1, target.height, target.width), dtype=torch.uint8)
    if parameters.rotation_degrees:
        # rotate は tensor 入力で nearest-exact を受け付けない。mask は resize 後の
        # 全 true から作るので、補間が問題になるのは回転だけであり nearest で足りる。
        valid = transforms.rotate(
            valid,
            parameters.rotation_degrees,
            interpolation=InterpolationMode.NEAREST,
        )
    return valid > 0


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


def _validate_view_stacks(views: Sequence[Sequence[Tensor]]) -> str | None:
    if not views:
        return "view は 1 個以上必要です"
    if error := _validate_image_stack([image for view in views for image in view]):
        return error
    counts = sorted({len(view) for view in views})
    if len(counts) != 1:
        return f"view ごとの画像枚数がそろっていません: {counts}"
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
    "PreprocessedMultiViewSample",
    "PreprocessedSample",
    "decode_rgb_image",
    "sample_layer_norm",
]
