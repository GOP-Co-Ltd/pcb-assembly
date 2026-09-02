"""Dataset-independent paired-image preprocessing."""

from __future__ import annotations

import hashlib
import math
import random
from typing import Protocol

import attrs
import numpy as np
import torch
from torch import Tensor
from torchvision import tv_tensors
from torchvision.transforms import InterpolationMode
from torchvision.transforms.v2 import functional as tvf


class ImageShapeSample(Protocol):
    """Minimum sample metadata needed to plan image preprocessing."""

    @property
    def sample_id(self) -> str: ...

    @property
    def width(self) -> int: ...

    @property
    def height(self) -> int: ...


@attrs.frozen
class ImageConstraints:
    """Input limits applied without modifying source images."""

    min_size: int = 32
    max_size: int = 1024
    max_pixels: int = 262_144
    stride: int = 32
    normalization_epsilon: float = 1e-5

    def __attrs_post_init__(self) -> None:
        if self.min_size < 1 or self.max_size < self.min_size:
            raise ValueError("image min/max sizeが不正です")
        if self.max_pixels < self.min_size * self.min_size:
            raise ValueError("max_pixelsがmin_sizeに対して小さすぎます")
        if self.stride < 1:
            raise ValueError("strideは正の整数が必要です")
        if (
            not math.isfinite(self.normalization_epsilon)
            or self.normalization_epsilon <= 0
        ):
            raise ValueError("normalization_epsilonは正の有限値が必要です")

    def to_dict(self) -> dict[str, object]:
        return attrs.asdict(self)


@attrs.frozen
class AugmentationConfig:
    """Rotation and isotropic scale augmentation settings."""

    enabled: bool = True
    min_scale: float = 0.8
    max_scale: float = 1.2

    def __attrs_post_init__(self) -> None:
        if (
            not math.isfinite(self.min_scale)
            or not math.isfinite(self.max_scale)
            or self.min_scale <= 0
            or self.max_scale < self.min_scale
        ):
            raise ValueError("augmentation scale範囲が不正です")


@attrs.frozen
class PreprocessedImagePair:
    image_6ch: Tensor
    valid_pixel_mask: Tensor
    pixel_per_mm: float
    mean_sample: float
    variance_sample: float


def _as_rgb_chw(image: Tensor | np.ndarray, label: str) -> Tensor:
    if isinstance(image, np.ndarray):
        if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
            raise ValueError(f"{label}はRGB uint8 HWC arrayが必要です")
        tensor = tvf.to_image(np.ascontiguousarray(image))
    elif isinstance(image, Tensor):
        tensor = image
    else:
        raise TypeError(f"{label}はTensorまたはnumpy arrayが必要です")
    if tensor.dtype != torch.uint8 or tensor.ndim != 3 or tensor.shape[0] != 3:
        raise ValueError(f"{label}はRGB uint8 [3,H,W] tensorが必要です")
    return tensor


def _constrained_shape(
    height: int, width: int, scale: float, constraints: ImageConstraints
) -> tuple[int, int, float]:
    """Return constrained dimensions and the isotropic scale actually used."""

    if height < constraints.min_size or width < constraints.min_size:
        raise ValueError(
            f"source imageは各辺{constraints.min_size}px以上が必要です: "
            f"{width}x{height}"
        )
    minimum_scale = max(constraints.min_size / height, constraints.min_size / width)
    actual_scale = max(scale, minimum_scale)
    scaled_height = max(1, math.floor(height * actual_scale))
    scaled_width = max(1, math.floor(width * actual_scale))
    downscale = min(
        1.0,
        constraints.max_size / scaled_height,
        constraints.max_size / scaled_width,
        math.sqrt(constraints.max_pixels / (scaled_height * scaled_width)),
    )
    output_height = max(constraints.min_size, math.floor(scaled_height * downscale))
    output_width = max(constraints.min_size, math.floor(scaled_width * downscale))
    while output_height * output_width > constraints.max_pixels:
        if output_height >= output_width:
            output_height -= 1
        else:
            output_width -= 1
    return (
        output_height,
        output_width,
        (output_height / height + output_width / width) / 2,
    )


def augmentation_parameters(
    sample_id: str,
    *,
    global_seed: int,
    epoch: int,
    augmentation: AugmentationConfig,
) -> tuple[float, float]:
    """Derive worker-independent augmentation parameters for one
    sample/epoch."""

    if not augmentation.enabled:
        return 0.0, 1.0
    digest = hashlib.sha256(
        f"{global_seed}:{epoch}:{sample_id}:augmentation".encode()
    ).digest()
    generator = random.Random(int.from_bytes(digest[:8], "big"))
    angle = generator.random() * 360.0
    log_scale = generator.uniform(
        math.log(augmentation.min_scale), math.log(augmentation.max_scale)
    )
    return angle, math.exp(log_scale)


def _rotate(image: Tensor, angle_degrees: float, *, image_data: bool) -> Tensor:
    if angle_degrees == 0:
        return image
    transform_input = image if image_data else tv_tensors.Mask(image)
    transformed = tvf.affine(
        transform_input,
        angle=angle_degrees,
        translate=[0, 0],
        scale=1.0,
        shear=[0.0, 0.0],
        interpolation=(
            InterpolationMode.BILINEAR if image_data else InterpolationMode.NEAREST
        ),
        fill=[0.0],
    )
    return transformed.as_subclass(Tensor)


def preprocess_rgb_pair(
    pre_rgb: Tensor | np.ndarray,
    post_rgb: Tensor | np.ndarray,
    pixel_per_mm: float,
    *,
    constraints: ImageConstraints = ImageConstraints(),
    angle_degrees: float = 0.0,
    scale: float = 1.0,
    geometry_mask: Tensor | None = None,
) -> PreprocessedImagePair:
    """Resize and rotate an RGB pair, then standardize it with one scalar."""

    pre = _as_rgb_chw(pre_rgb, "pre")
    post = _as_rgb_chw(post_rgb, "post")
    if pre.shape != post.shape:
        raise ValueError("pre/post RGBのshapeが一致しません")
    if not math.isfinite(pixel_per_mm) or pixel_per_mm <= 0:
        raise ValueError(f"pixel_per_mmは正の有限値が必要です: {pixel_per_mm!r}")
    if not math.isfinite(angle_degrees) or not math.isfinite(scale) or scale <= 0:
        raise ValueError("rotation/scale parameterが不正です")
    height, width = int(pre.shape[1]), int(pre.shape[2])
    output_height, output_width, actual_scale = _constrained_shape(
        height, width, scale, constraints
    )
    valid = torch.ones((1, height, width), dtype=torch.uint8, device=pre.device)
    if geometry_mask is not None:
        if geometry_mask.dtype != torch.uint8 or geometry_mask.shape != (
            1,
            height,
            width,
        ):
            raise ValueError("geometry maskはuint8 [1,H,W]が必要です")
        if not all(int(value) in (0, 255) for value in torch.unique(geometry_mask)):
            raise ValueError("geometry maskは0/255だけで構成する必要があります")
    pre = _rotate(pre, angle_degrees, image_data=True)
    post = _rotate(post, angle_degrees, image_data=True)
    valid = _rotate(valid, angle_degrees, image_data=False)
    if geometry_mask is not None:
        geometry_mask = _rotate(geometry_mask, angle_degrees, image_data=False)
    size = [output_height, output_width]
    if tuple(pre.shape[1:]) != (output_height, output_width):
        pre = tvf.resize(
            pre, size, interpolation=InterpolationMode.BILINEAR, antialias=True
        )
        post = tvf.resize(
            post, size, interpolation=InterpolationMode.BILINEAR, antialias=True
        )
        valid = tvf.resize(valid, size, interpolation=InterpolationMode.NEAREST_EXACT)
        if geometry_mask is not None:
            geometry_mask = tvf.resize(
                geometry_mask, size, interpolation=InterpolationMode.NEAREST_EXACT
            )
    valid_mask = valid.to(torch.bool)
    if geometry_mask is not None:
        if not all(int(value) in (0, 255) for value in torch.unique(geometry_mask)):
            raise ValueError("変換後のgeometry maskは0/255である必要があります")
        geometry_region = geometry_mask.to(torch.bool)
        if not torch.any(geometry_region):
            raise ValueError("変換後のgeometry maskが空です")
        if torch.any(geometry_region & ~valid_mask):
            raise ValueError("geometry maskがsample有効領域からはみ出しています")
    pre_float = tvf.to_dtype(pre, torch.float32, scale=True)
    post_float = tvf.to_dtype(post, torch.float32, scale=True)
    image_6ch = torch.cat((pre_float, post_float), dim=0)
    valid_values = image_6ch.masked_select(valid_mask.expand_as(image_6ch))
    if valid_values.numel() == 0:
        raise ValueError("SampleLayerNormの有効画素が0です")
    mean = valid_values.mean()
    variance = valid_values.var(correction=0)
    if not torch.isfinite(mean) or not torch.isfinite(variance):
        raise ValueError("SampleLayerNorm統計が非有限です")
    if float(variance) < 1e-12:
        raise ValueError("SampleLayerNorm対象画像の分散が小さすぎます")
    normalized = (image_6ch - mean) / torch.sqrt(
        variance + constraints.normalization_epsilon
    )
    normalized = normalized.masked_fill(~valid_mask.expand_as(normalized), 0.0)
    return PreprocessedImagePair(
        image_6ch=normalized,
        valid_pixel_mask=valid_mask,
        pixel_per_mm=float(pixel_per_mm) * actual_scale,
        mean_sample=float(mean),
        variance_sample=float(variance),
    )


def preprocessed_shape(
    sample: ImageShapeSample,
    *,
    constraints: ImageConstraints = ImageConstraints(),
    training: bool = False,
    global_seed: int = 0,
    epoch: int = 0,
    augmentation: AugmentationConfig = AugmentationConfig(),
) -> tuple[int, int]:
    """Return the deterministic output shape for one sample/epoch."""

    _, scale = augmentation_parameters(
        sample.sample_id,
        global_seed=global_seed,
        epoch=epoch,
        augmentation=(
            augmentation if training else attrs.evolve(augmentation, enabled=False)
        ),
    )
    height, width, _ = _constrained_shape(
        sample.height, sample.width, scale, constraints
    )
    return height, width


__all__ = [
    "AugmentationConfig",
    "ImageConstraints",
    "ImageShapeSample",
    "PreprocessedImagePair",
    "augmentation_parameters",
    "preprocess_rgb_pair",
    "preprocessed_shape",
]
