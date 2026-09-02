"""Dataset-independent variable-image and pixel-budget batching."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Iterator, Sequence
from typing import override

import attrs
import torch
from torch import Tensor
from torch.utils.data import Sampler

from .image import (
    AugmentationConfig,
    ImageConstraints,
    ImageShapeSample,
    preprocessed_shape,
)


@attrs.frozen
class PaddedImageBatch:
    images: Tensor
    valid_pixel_masks: Tensor


def _ceil_to(value: int, multiple: int) -> int:
    return ((value + multiple - 1) // multiple) * multiple


def pad_image_samples(
    images: Sequence[Tensor],
    valid_pixel_masks: Sequence[Tensor],
    placement_seeds: Sequence[int],
    *,
    training: bool = False,
    stride: int = 32,
) -> PaddedImageBatch:
    """Pad variable-size CHW images and masks to one stride-aligned batch."""

    if not images:
        raise ValueError("空batchはcollateできません")
    if len(images) != len(valid_pixel_masks) or len(images) != len(placement_seeds):
        raise ValueError("image、mask、placement seedの件数が一致しません")
    if stride < 1:
        raise ValueError("strideは正の整数が必要です")
    channels = int(images[0].shape[0])
    device = images[0].device
    dtype = images[0].dtype
    for image, mask in zip(images, valid_pixel_masks, strict=True):
        if image.ndim != 3 or int(image.shape[0]) != channels:
            raise ValueError("batch imageは同じchannel数のCHW tensorが必要です")
        if image.device != device or image.dtype != dtype:
            raise ValueError("batch imageのdeviceとdtypeは統一してください")
        if mask.dtype != torch.bool or mask.shape != (
            1,
            image.shape[1],
            image.shape[2],
        ):
            raise ValueError("valid pixel maskはbool [1,H,W]が必要です")
        if mask.device != device:
            raise ValueError("imageとvalid pixel maskのdeviceが一致しません")
    max_height = _ceil_to(max(int(image.shape[1]) for image in images), stride)
    max_width = _ceil_to(max(int(image.shape[2]) for image in images), stride)
    padded_images = torch.zeros(
        (len(images), channels, max_height, max_width), dtype=dtype, device=device
    )
    padded_masks = torch.zeros(
        (len(images), 1, max_height, max_width), dtype=torch.bool, device=device
    )
    for index, (image, mask, placement_seed) in enumerate(
        zip(images, valid_pixel_masks, placement_seeds, strict=True)
    ):
        height, width = int(image.shape[1]), int(image.shape[2])
        if training:
            generator = random.Random(placement_seed)
            top = generator.randrange(max_height - height + 1)
            left = generator.randrange(max_width - width + 1)
        else:
            top = (max_height - height) // 2
            left = (max_width - width) // 2
        padded_images[index, :, top : top + height, left : left + width] = image
        padded_masks[index, :, top : top + height, left : left + width] = mask
    return PaddedImageBatch(padded_images, padded_masks)


class PixelBudgetBatchSampler(Sampler[list[int]]):
    """Deterministic aspect/area buckets constrained by padded pixel cost."""

    def __init__(
        self,
        samples: Sequence[ImageShapeSample],
        *,
        max_batch_pixels: int = 8_388_608,
        max_batch_size: int = 32,
        constraints: ImageConstraints = ImageConstraints(),
        training: bool = True,
        global_seed: int = 0,
        augmentation: AugmentationConfig = AugmentationConfig(),
    ) -> None:
        if max_batch_pixels < 1 or max_batch_size < 1:
            raise ValueError("batch pixel budgetとbatch sizeは正の整数が必要です")
        self._samples = tuple(samples)
        self._max_batch_pixels = max_batch_pixels
        self._max_batch_size = max_batch_size
        self._constraints = constraints
        self._training = training
        self._global_seed = global_seed
        self._augmentation = augmentation
        self._epoch = 0
        self._cached_plan: tuple[tuple[int, ...], ...] | None = None

    def set_epoch(self, epoch: int) -> None:
        if epoch < 0:
            raise ValueError("epochは0以上が必要です")
        self._epoch = epoch
        self._cached_plan = None

    def state_dict(self) -> dict[str, object]:
        return {
            "epoch": self._epoch,
            "global_seed": self._global_seed,
            "batch_plan": [list(batch) for batch in self.plan_for_epoch(self._epoch)],
        }

    def plan_for_epoch(self, epoch: int) -> tuple[tuple[int, ...], ...]:
        buckets: dict[tuple[int, int], list[tuple[int, int, int]]] = defaultdict(list)
        for index, sample in enumerate(self._samples):
            height, width = preprocessed_shape(
                sample,
                constraints=self._constraints,
                training=self._training,
                global_seed=self._global_seed,
                epoch=epoch,
                augmentation=self._augmentation,
            )
            aspect_bucket = round(math.log2(width / height) / 0.25)
            area_bucket = round(math.log2(width * height))
            buckets[(aspect_bucket, area_bucket)].append((index, height, width))
        generator = random.Random(f"{self._global_seed}:{epoch}:batch-plan")
        bucket_keys = sorted(buckets)
        generator.shuffle(bucket_keys)
        plan: list[tuple[int, ...]] = []
        for key in bucket_keys:
            entries = buckets[key]
            generator.shuffle(entries)
            batch: list[tuple[int, int, int]] = []
            for entry in entries:
                candidate = [*batch, entry]
                max_height = _ceil_to(
                    max(item[1] for item in candidate), self._constraints.stride
                )
                max_width = _ceil_to(
                    max(item[2] for item in candidate), self._constraints.stride
                )
                cost = len(candidate) * max_height * max_width
                if batch and (
                    len(candidate) > self._max_batch_size
                    or cost > self._max_batch_pixels
                ):
                    plan.append(tuple(item[0] for item in batch))
                    batch = [entry]
                else:
                    batch = candidate
                single_cost = _ceil_to(entry[1], self._constraints.stride) * _ceil_to(
                    entry[2], self._constraints.stride
                )
                if single_cost > self._max_batch_pixels:
                    raise ValueError(
                        "1 sampleがbatch pixel budgetを超えます: "
                        f"{self._samples[entry[0]].sample_id}"
                    )
            if batch:
                plan.append(tuple(item[0] for item in batch))
        return tuple(plan)

    @override
    def __iter__(self) -> Iterator[list[int]]:
        if self._cached_plan is None:
            self._cached_plan = self.plan_for_epoch(self._epoch)
        return iter([list(batch) for batch in self._cached_plan])

    def __len__(self) -> int:
        if self._cached_plan is None:
            self._cached_plan = self.plan_for_epoch(self._epoch)
        return len(self._cached_plan)


__all__ = [
    "PaddedImageBatch",
    "PixelBudgetBatchSampler",
    "pad_image_samples",
]
