"""GroupNormを使う小型ResNet向けの構築要素."""

from __future__ import annotations

from typing import override

from torch import Tensor, nn


def build_group_norm_conv(
    in_channels: int,
    out_channels: int,
    *,
    stride: int,
    groups: int,
    kernel_size: int = 3,
) -> nn.Sequential:
    """``Conv2d -> GroupNorm -> ReLU`` を構築する."""

    if in_channels < 1 or out_channels < 1:
        raise ValueError("convolution channels must be positive")
    if stride < 1 or kernel_size < 1:
        raise ValueError("convolution stride and kernel size must be positive")
    if groups < 1 or out_channels % groups:
        raise ValueError("output channels must be divisible by GroupNorm groups")
    return nn.Sequential(
        nn.Conv2d(
            in_channels,
            out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=kernel_size // 2,
            bias=False,
        ),
        nn.GroupNorm(groups, out_channels, eps=1e-5, affine=True),
        nn.ReLU(inplace=True),
    )


class GroupNormResidualBlock(nn.Module):
    """2個の3x3 convolutionと必要時のprojectionを持つresidual block."""

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        *,
        stride: int,
        groups: int,
    ) -> None:
        super().__init__()
        self._conv1 = build_group_norm_conv(
            in_channels,
            out_channels,
            stride=stride,
            groups=groups,
        )
        self._conv2 = nn.Sequential(
            nn.Conv2d(
                out_channels,
                out_channels,
                kernel_size=3,
                stride=1,
                padding=1,
                bias=False,
            ),
            nn.GroupNorm(groups, out_channels, eps=1e-5, affine=True),
        )
        if stride == 1 and in_channels == out_channels:
            self._projection: nn.Module = nn.Identity()
        else:
            self._projection = nn.Sequential(
                nn.Conv2d(
                    in_channels,
                    out_channels,
                    kernel_size=1,
                    stride=stride,
                    bias=False,
                ),
                nn.GroupNorm(groups, out_channels, eps=1e-5, affine=True),
            )
        self._activation = nn.ReLU(inplace=True)

    @override
    def forward(self, inputs: Tensor) -> Tensor:
        residual = self._projection(inputs)
        return self._activation(self._conv2(self._conv1(inputs)) + residual)


def build_group_norm_residual_stage(
    in_channels: int,
    out_channels: int,
    *,
    block_count: int,
    first_stride: int,
    groups: int,
) -> nn.Sequential:
    """先頭blockだけshapeを変更するresidual stageを構築する."""

    if block_count < 1:
        raise ValueError("residual stage block_count must be positive")
    blocks: list[nn.Module] = [
        GroupNormResidualBlock(
            in_channels,
            out_channels,
            stride=first_stride,
            groups=groups,
        )
    ]
    blocks.extend(
        GroupNormResidualBlock(
            out_channels,
            out_channels,
            stride=1,
            groups=groups,
        )
        for _ in range(block_count - 1)
    )
    return nn.Sequential(*blocks)


__all__ = [
    "GroupNormResidualBlock",
    "build_group_norm_conv",
    "build_group_norm_residual_stage",
]
