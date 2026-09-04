"""画像 encoder を構成する畳み込み部品.

正規化は GroupNorm だけを使う。

batch 統計に依存しないので、batch size 1 でも値が変わらない。

可変サイズ画像を小さな batch へ詰める学習方式と両立させるため。

padding された領域は学習可能な padding pixel へ置き換える。

padding 値ではなく「画像ではない」ことを学習させるため。
"""

from __future__ import annotations

from typing import override

import attrs
import torch
from torch import Tensor, nn

_GROUP_NORM_EPS = 1e-5


def build_group_norm_convolution(
    in_channels: int,
    out_channels: int,
    *,
    stride: int,
    groups: int,
    kernel_size: int = 3,
) -> nn.Sequential:
    """Conv2d -> GroupNorm -> ReLU を 1 単位として組む.

    Conv2d の bias は直後の GroupNorm が打ち消すため持たせない。
    """

    if in_channels < 1 or out_channels < 1:
        raise ValueError(
            f"channel 数は正の整数が必要です: {in_channels} -> {out_channels}"
        )
    if stride < 1:
        raise ValueError(f"stride は正の整数が必要です: {stride}")
    if groups < 1 or out_channels % groups != 0:
        raise ValueError(
            f"out_channels は groups で割り切れる必要があります: "
            f"{out_channels} % {groups}"
        )
    return nn.Sequential(
        nn.Conv2d(
            in_channels,
            out_channels,
            kernel_size,
            stride=stride,
            padding=kernel_size // 2,
            bias=False,
        ),
        nn.GroupNorm(groups, out_channels, eps=_GROUP_NORM_EPS, affine=True),
        nn.ReLU(inplace=True),
    )


class GroupNormResidualBlock(nn.Module):
    """3x3 畳み込み 2 段の residual block.

    入出力の shape が変わらないときは shortcut に projection を置かない。
    """

    def __init__(
        self, in_channels: int, out_channels: int, *, stride: int, groups: int
    ) -> None:
        super().__init__()
        self._entry = build_group_norm_convolution(
            in_channels, out_channels, stride=stride, groups=groups
        )
        self._convolution = nn.Conv2d(
            out_channels, out_channels, 3, stride=1, padding=1, bias=False
        )
        self._normalization = nn.GroupNorm(
            groups, out_channels, eps=_GROUP_NORM_EPS, affine=True
        )
        self._shortcut = (
            None
            if stride == 1 and in_channels == out_channels
            else nn.Sequential(
                nn.Conv2d(in_channels, out_channels, 1, stride=stride, bias=False),
                nn.GroupNorm(groups, out_channels, eps=_GROUP_NORM_EPS, affine=True),
            )
        )
        self._activation = nn.ReLU(inplace=True)

    @override
    def forward(self, inputs: Tensor) -> Tensor:
        shortcut = inputs if self._shortcut is None else self._shortcut(inputs)
        residual = self._normalization(self._convolution(self._entry(inputs)))
        return self._activation(residual + shortcut)


def build_group_norm_residual_stage(
    in_channels: int,
    out_channels: int,
    *,
    block_count: int,
    first_stride: int,
    groups: int,
) -> nn.Sequential:
    """同じ channel 数の residual block を重ねた 1 stage を組む.

    解像度と channel 数を変えるのは先頭 block だけとする。
    """

    if block_count < 1:
        raise ValueError(f"block_count は正の整数が必要です: {block_count}")
    blocks: list[nn.Module] = [
        GroupNormResidualBlock(
            in_channels, out_channels, stride=first_stride, groups=groups
        )
    ]
    blocks.extend(
        GroupNormResidualBlock(out_channels, out_channels, stride=1, groups=groups)
        for _ in range(block_count - 1)
    )
    return nn.Sequential(*blocks)


def replace_invalid_pixels(
    images: Tensor, valid_pixel_mask: Tensor, fill: Tensor
) -> Tensor:
    """Padding 領域の画素を学習可能な値へ置き換える.

    ``fill`` は ``[1, C, 1, 1]`` で、batch と空間方向へ broadcast する。

    乗算ではなく ``torch.where`` を使う。

    padding 側に非有限値が入っていても伝播させないため。
    """

    return torch.where(valid_pixel_mask, images, fill)


@attrs.frozen
class ImageEncoderConfig:
    """畳み込み encoder の形を決める設定.

    channel 数と stride はすべてタプルで受け取る。

    どの深さでどれだけ縮めるかはドメイン側の判断とする。

    ``ml`` 側が持つ既定値は ``group_norm_groups`` だけとする。
    """

    input_channels: int
    stem_channels: tuple[int, ...]
    stem_strides: tuple[int, ...]
    stage_channels: tuple[int, ...]
    stage_strides: tuple[int, ...]
    blocks_per_stage: tuple[int, ...]
    group_norm_groups: int = 8

    def validate(self) -> str | None:
        """Encoder 設定の整合を検証する."""

        if self.input_channels < 1:
            return f"input_channels は正の整数が必要です: {self.input_channels}"
        if self.group_norm_groups < 1:
            return f"group_norm_groups は正の整数が必要です: {self.group_norm_groups}"
        if not self.stem_channels or not self.stage_channels:
            return "stem と stage はそれぞれ 1 段以上が必要です"
        if len(self.stem_channels) != len(self.stem_strides):
            return (
                "stem_channels と stem_strides の長さが一致しません: "
                f"{len(self.stem_channels)} と {len(self.stem_strides)}"
            )
        if len(self.stage_channels) != len(self.stage_strides) or len(
            self.stage_channels
        ) != len(self.blocks_per_stage):
            return (
                "stage_channels、stage_strides、blocks_per_stage の長さが"
                f"一致しません: {len(self.stage_channels)}、"
                f"{len(self.stage_strides)}、{len(self.blocks_per_stage)}"
            )
        for channels in (*self.stem_channels, *self.stage_channels):
            if channels < 1:
                return f"channel 数は正の整数が必要です: {channels}"
            if channels % self.group_norm_groups != 0:
                return (
                    "channel 数は group_norm_groups で割り切れる必要があります: "
                    f"{channels} % {self.group_norm_groups}"
                )
        for stride in (*self.stem_strides, *self.stage_strides):
            if stride < 1:
                return f"stride は正の整数が必要です: {stride}"
        for block_count in self.blocks_per_stage:
            if block_count < 1:
                return f"blocks_per_stage は正の整数が必要です: {block_count}"
        return None

    @property
    def output_features(self) -> int:
        """Pooling 後の feature 次元数."""

        return self.stage_channels[-1]

    @property
    def total_stride(self) -> int:
        """入力から最終 feature map までの縮小率."""

        total = 1
        for stride in (*self.stem_strides, *self.stage_strides):
            total *= stride
        return total


class ImageEncoder(nn.Module):
    """可変サイズ画像を固定長 feature へ落とす畳み込み encoder.

    global average pooling で終わるので、出力次元は入力の高さ・幅に依らない。

    有効画素 mask があるときは、stem へ入れる前に padding 領域を置き換える。
    """

    def __init__(self, config: ImageEncoderConfig) -> None:
        super().__init__()
        if error := config.validate():
            raise ValueError(error)
        self._config = config
        channels = config.input_channels
        stem: list[nn.Module] = []
        for out_channels, stride in zip(
            config.stem_channels, config.stem_strides, strict=True
        ):
            stem.append(
                build_group_norm_convolution(
                    channels,
                    out_channels,
                    stride=stride,
                    groups=config.group_norm_groups,
                )
            )
            channels = out_channels
        stages: list[nn.Module] = []
        for out_channels, stride, block_count in zip(
            config.stage_channels,
            config.stage_strides,
            config.blocks_per_stage,
            strict=True,
        ):
            stages.append(
                build_group_norm_residual_stage(
                    channels,
                    out_channels,
                    block_count=block_count,
                    first_stride=stride,
                    groups=config.group_norm_groups,
                )
            )
            channels = out_channels
        self._stem = nn.Sequential(*stem)
        self._stages = nn.Sequential(*stages)
        self._pool = nn.AdaptiveAvgPool2d((1, 1))
        self._padding_pixel = nn.Parameter(torch.zeros(1, config.input_channels, 1, 1))

    @property
    def output_features(self) -> int:
        """Forward が返す feature 次元数."""

        return self._config.output_features

    @property
    def padding_pixel(self) -> Tensor:
        """Padding 領域へ入れる学習可能な画素値 ``[1, C, 1, 1]``.

        勾配が流れていることを外から観測できるよう公開する。

        ``.grad`` を見せる必要があるので ``detach()`` した複製は返さない。
        """

        return self._padding_pixel

    @override
    def forward(self, images: Tensor, valid_pixel_mask: Tensor | None = None) -> Tensor:
        """``[B, C, H, W]`` を ``[B, output_features]`` へ変換する."""

        self._reject_invalid_inputs(images, valid_pixel_mask)
        if valid_pixel_mask is not None:
            images = replace_invalid_pixels(
                images, valid_pixel_mask, self._padding_pixel
            )
        pooled = self._pool(self._stages(self._stem(images)))
        return torch.flatten(pooled, start_dim=1)

    def _reject_invalid_inputs(
        self, images: Tensor, valid_pixel_mask: Tensor | None
    ) -> None:
        if images.ndim != 4:
            raise ValueError(
                f"images は [B, C, H, W] が必要です: {tuple(images.shape)}"
            )
        if int(images.shape[1]) != self._config.input_channels:
            raise ValueError(
                "images の channel 数が config と一致しません: "
                f"{int(images.shape[1])}（期待値 {self._config.input_channels}）"
            )
        if valid_pixel_mask is None:
            return
        if valid_pixel_mask.dtype != torch.bool:
            raise ValueError(
                f"valid pixel mask は bool が必要です: {valid_pixel_mask.dtype}"
            )
        expected = (int(images.shape[0]), 1, int(images.shape[2]), int(images.shape[3]))
        if tuple(valid_pixel_mask.shape) != expected:
            raise ValueError(
                f"valid pixel mask は {expected} が必要です: "
                f"{tuple(valid_pixel_mask.shape)}"
            )


__all__ = [
    "GroupNormResidualBlock",
    "ImageEncoder",
    "ImageEncoderConfig",
    "build_group_norm_convolution",
    "build_group_norm_residual_stage",
    "replace_invalid_pixels",
]
