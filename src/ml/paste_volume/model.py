"""画像ペアからペースト体積と不確かさを推定する小型ResNet."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import override

import torch
from torch import Tensor, nn
from torch.nn import functional as F

from ml.model.residual import (
    build_group_norm_conv,
    build_group_norm_residual_stage,
)


@dataclass(frozen=True)
class PasteVolumeModelConfig:
    """永続化可能なv1モデル構成."""

    family: str = "paste-volume-resnet-small-v1"
    input_channels: int = 6
    stem_channels: tuple[int, int, int] = (24, 32, 48)
    stage_channels: tuple[int, int, int] = (48, 96, 160)
    blocks_per_stage: tuple[int, int, int] = (2, 2, 2)
    group_norm_groups: int = 8
    hidden_features: int = 128
    log_variance_min: float = -14.0
    log_variance_max: float = 5.0

    def __post_init__(self) -> None:
        if self.family != "paste-volume-resnet-small-v1":
            raise ValueError(f"unsupported model family: {self.family}")
        if self.input_channels != 6:
            raise ValueError("paste-volume v1 requires exactly 6 input channels")
        if len(self.stem_channels) != 3 or len(self.stage_channels) != 3:
            raise ValueError("paste-volume v1 requires exactly three encoder stages")
        if len(self.blocks_per_stage) != 3 or any(
            block_count <= 0 for block_count in self.blocks_per_stage
        ):
            raise ValueError("blocks_per_stage must contain three positive values")
        if self.group_norm_groups not in (1, 4, 8):
            raise ValueError("group_norm_groups must be one of 1, 4, or 8")
        channels = (*self.stem_channels, *self.stage_channels)
        if any(channel % self.group_norm_groups for channel in channels):
            raise ValueError(
                "all encoder channels must be divisible by GroupNorm groups"
            )
        if self.hidden_features <= 0:
            raise ValueError("hidden_features must be positive")
        if self.log_variance_min >= self.log_variance_max:
            raise ValueError("log-variance bounds must be increasing")

    def to_dict(self) -> dict[str, object]:
        """Checkpoint/export用のplain dictionaryへ変換する."""

        return asdict(self)


class PasteVolumeResNet(nn.Module):
    """可変画像shape、batch size非依存の体積回帰モデル."""

    def __init__(self, config: PasteVolumeModelConfig | None = None) -> None:
        super().__init__()
        self._config = config or PasteVolumeModelConfig()
        stem_channels = self._config.stem_channels
        self._padding_pixel = nn.Parameter(torch.zeros(1, 6, 1, 1))
        stem: list[nn.Module] = []
        in_channels = self._config.input_channels
        for out_channels in stem_channels:
            stem.append(
                build_group_norm_conv(
                    in_channels,
                    out_channels,
                    stride=2,
                    groups=self._config.group_norm_groups,
                )
            )
            in_channels = out_channels
        self._stem = nn.Sequential(*stem)
        stages: list[nn.Module] = []
        for stage_index, (out_channels, block_count) in enumerate(
            zip(
                self._config.stage_channels,
                self._config.blocks_per_stage,
                strict=True,
            )
        ):
            stage_stride = 1 if stage_index == 0 else 2
            stages.append(
                build_group_norm_residual_stage(
                    in_channels,
                    out_channels,
                    block_count=block_count,
                    first_stride=stage_stride,
                    groups=self._config.group_norm_groups,
                )
            )
            in_channels = out_channels
        self._stages = nn.ModuleList(stages)
        self._pool = nn.AdaptiveAvgPool2d((1, 1))
        self._features = nn.Sequential(
            nn.Linear(in_channels + 1, self._config.hidden_features),
            nn.ReLU(inplace=True),
        )
        self._mean_head = nn.Linear(self._config.hidden_features, 1)
        self._log_variance_head = nn.Linear(self._config.hidden_features, 1)

    @property
    def config(self) -> PasteVolumeModelConfig:
        """永続化するモデル構成を返す."""

        return self._config

    @override
    def forward(
        self,
        image_6ch: Tensor,
        valid_pixel_mask: Tensor,
        pixel_per_mm: Tensor,
    ) -> tuple[Tensor, Tensor]:
        if image_6ch.ndim != 4 or image_6ch.shape[1] != 6:
            raise ValueError("image_6ch must have shape [B, 6, H, W]")
        expected_mask = (image_6ch.shape[0], 1, *image_6ch.shape[2:])
        if tuple(valid_pixel_mask.shape) != expected_mask:
            raise ValueError("valid_pixel_mask must have shape [B, 1, H, W]")
        if valid_pixel_mask.dtype is not torch.bool:
            raise ValueError("valid_pixel_mask must be a boolean tensor")
        if pixel_per_mm.shape != (image_6ch.shape[0], 1):
            raise ValueError("pixel_per_mm must have shape [B, 1]")
        if not image_6ch.is_floating_point() or not pixel_per_mm.is_floating_point():
            raise ValueError("model inputs must be floating point tensors")
        mask = valid_pixel_mask.to(dtype=torch.bool)
        inputs = torch.where(mask, image_6ch, self._padding_pixel.to(image_6ch.dtype))
        encoded = self._stem(inputs)
        for stage in self._stages:
            encoded = stage(encoded)
        pooled = self._pool(encoded).flatten(1)
        features = self._features(torch.cat((pooled, torch.log(pixel_per_mm)), dim=1))
        mean = F.softplus(self._mean_head(features))
        log_variance = torch.clamp(
            self._log_variance_head(features),
            min=self._config.log_variance_min,
            max=self._config.log_variance_max,
        )
        return mean, log_variance

    def set_fine_tune_trainable(self, *, full_model: bool = False) -> None:
        """Pi fine-tuning既定の更新対象だけを有効にする."""

        for parameter in self.parameters():
            parameter.requires_grad = full_model
        if full_model:
            return
        self._padding_pixel.requires_grad = True
        for parameter in self._stages[-1].parameters():
            parameter.requires_grad = True
        for module in (self._features, self._mean_head, self._log_variance_head):
            for parameter in module.parameters():
                parameter.requires_grad = True


def validate_model_inputs(
    image_6ch: Tensor,
    valid_pixel_mask: Tensor,
    pixel_per_mm: Tensor,
) -> None:
    """compile境界の外でmodel入力値を検証する."""

    if not torch.all(torch.isfinite(image_6ch)).item():
        raise ValueError("image_6ch must contain finite values")
    if (
        not torch.all(torch.isfinite(pixel_per_mm)).item()
        or torch.any(pixel_per_mm <= 0).item()
    ):
        raise ValueError("pixel_per_mm must contain positive finite values")
    if not torch.any(valid_pixel_mask).item():
        raise ValueError("valid_pixel_mask must contain at least one valid pixel")


def model_multiply_accumulate_count(
    config: PasteVolumeModelConfig,
    *,
    height: int,
    width: int,
) -> int:
    """1 sampleの畳み込みと線形層のmultiply-accumulate数を返す."""

    if height < 1 or width < 1:
        raise ValueError("height and width must be positive")

    macs = 0
    in_channels = config.input_channels
    current_height = height
    current_width = width

    def add_convolution(
        input_channels: int,
        output_channels: int,
        *,
        kernel_size: int,
        stride: int,
    ) -> tuple[int, int]:
        nonlocal macs, current_height, current_width
        output_height = (current_height + stride - 1) // stride
        output_width = (current_width + stride - 1) // stride
        macs += (
            output_height
            * output_width
            * output_channels
            * input_channels
            * kernel_size
            * kernel_size
        )
        return output_height, output_width

    for output_channels in config.stem_channels:
        current_height, current_width = add_convolution(
            in_channels, output_channels, kernel_size=3, stride=2
        )
        in_channels = output_channels

    for stage_index, (output_channels, block_count) in enumerate(
        zip(config.stage_channels, config.blocks_per_stage, strict=True)
    ):
        first_stride = 1 if stage_index == 0 else 2
        input_channels = in_channels
        input_height = current_height
        input_width = current_width
        current_height, current_width = add_convolution(
            input_channels,
            output_channels,
            kernel_size=3,
            stride=first_stride,
        )
        add_convolution(output_channels, output_channels, kernel_size=3, stride=1)
        if first_stride != 1 or input_channels != output_channels:
            macs += current_height * current_width * output_channels * input_channels
        for _ in range(block_count - 1):
            add_convolution(output_channels, output_channels, kernel_size=3, stride=1)
            add_convolution(output_channels, output_channels, kernel_size=3, stride=1)
        in_channels = output_channels
        if input_height < current_height or input_width < current_width:
            raise AssertionError("encoder unexpectedly increased spatial dimensions")

    macs += (in_channels + 1) * config.hidden_features
    macs += config.hidden_features * 2
    return macs


def model_gmac(
    config: PasteVolumeModelConfig,
    *,
    height: int = 512,
    width: int = 512,
) -> float:
    """指定shapeのmodel規模を10億multiply-accumulate単位で返す."""

    return (
        model_multiply_accumulate_count(config, height=height, width=width)
        / 1_000_000_000.0
    )


__all__ = [
    "PasteVolumeModelConfig",
    "PasteVolumeResNet",
    "model_gmac",
    "model_multiply_accumulate_count",
    "validate_model_inputs",
]
