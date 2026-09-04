"""可変サイズ画像の batch 計画と padding.

固定枚数で batch を作ると、最大画像に引きずられて padding と memory が増える。

そこで aspect ratio と面積で bucket 化し、padding 後の画素数を予算にして詰める。

batch 計画は純関数として返す。

学習 loop はこの計画を checkpoint へ保存し、中断後は未処理の batch から再開する。
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Sequence

import attrs
import torch
from torch import Tensor

_ASPECT_BUCKET_STEP = 0.25
_AREA_BUCKET_STEP = 1.0


@attrs.frozen
class BatchShape:
    """Batch 計画に必要な、前処理後の 1 sample の形."""

    sample_id: str
    height: int
    width: int


@attrs.frozen
class PaddedBatch:
    """共通サイズへそろえた画像と、実画像の位置を示す mask."""

    images: Tensor = attrs.field(eq=False)
    valid_pixel_masks: Tensor = attrs.field(eq=False)


def plan_pixel_budget_batches(
    shapes: Sequence[BatchShape],
    *,
    max_batch_pixels: int,
    max_batch_size: int,
    stride: int,
    seed: int,
    epoch: int,
) -> tuple[tuple[str, ...], ...]:
    """1 epoch 分の batch を sample ID の並びとして決定論的に組む.

    同じ ``seed`` と ``epoch`` なら、入力の並び順が違っても同じ計画になる。
    最後の小さな batch も捨てない（encoder は GroupNorm なので batch size 1 でも
    正規化規則が変わらない）。
    """

    if max_batch_pixels < 1 or max_batch_size < 1:
        raise ValueError("batch pixel budget と batch size は正の整数が必要です")
    if stride < 1:
        raise ValueError("stride は正の整数が必要です")
    _reject_duplicate_sample_ids(shapes)

    buckets: dict[tuple[int, int], list[BatchShape]] = defaultdict(list)
    for shape in shapes:
        buckets[_bucket_key(shape)].append(shape)

    generator = random.Random(f"{seed}:{epoch}:batch-plan")
    keys = sorted(buckets)
    generator.shuffle(keys)
    plan: list[tuple[str, ...]] = []
    for key in keys:
        entries = sorted(buckets[key], key=lambda shape: shape.sample_id)
        generator.shuffle(entries)
        plan.extend(
            _fill_batches(
                entries,
                max_batch_pixels=max_batch_pixels,
                max_batch_size=max_batch_size,
                stride=stride,
            )
        )
    return tuple(plan)


def pad_image_samples(
    images: Sequence[Tensor],
    valid_masks: Sequence[Tensor],
    *,
    placement_seeds: Sequence[int],
    training: bool = False,
    stride: int = 32,
) -> PaddedBatch:
    """CHW 画像を batch 内の最大サイズへ stride 揃えで padding する.

    学習時は配置位置を ``placement_seeds`` から決定論的にずらし、評価時は中央へ
    置く。padding 値そのものには意味を持たせず、model 側が
    ``valid_pixel_masks`` を見て学習可能な padding pixel へ置き換える。
    """

    _validate_batch_inputs(images, valid_masks, placement_seeds, stride)
    channels = int(images[0].shape[0])
    height = _ceil_to(max(int(image.shape[1]) for image in images), stride)
    width = _ceil_to(max(int(image.shape[2]) for image in images), stride)
    padded_images = torch.zeros(
        (len(images), channels, height, width),
        dtype=images[0].dtype,
        device=images[0].device,
    )
    padded_masks = torch.zeros(
        (len(images), 1, height, width), dtype=torch.bool, device=images[0].device
    )
    for index, (image, mask, placement_seed) in enumerate(
        zip(images, valid_masks, placement_seeds, strict=True)
    ):
        top, left = _placement(
            image, height=height, width=width, seed=placement_seed, training=training
        )
        rows = slice(top, top + int(image.shape[1]))
        columns = slice(left, left + int(image.shape[2]))
        padded_images[index, :, rows, columns] = image
        padded_masks[index, :, rows, columns] = mask
    return PaddedBatch(images=padded_images, valid_pixel_masks=padded_masks)


def _bucket_key(shape: BatchShape) -> tuple[int, int]:
    aspect = round(math.log2(shape.width / shape.height) / _ASPECT_BUCKET_STEP)
    area = round(math.log2(shape.width * shape.height) / _AREA_BUCKET_STEP)
    return aspect, area


def _fill_batches(
    entries: Sequence[BatchShape],
    *,
    max_batch_pixels: int,
    max_batch_size: int,
    stride: int,
) -> list[tuple[str, ...]]:
    batches: list[tuple[str, ...]] = []
    batch: list[BatchShape] = []
    for entry in entries:
        alone = _ceil_to(entry.height, stride) * _ceil_to(entry.width, stride)
        if alone > max_batch_pixels:
            raise ValueError(
                f"1 sample が batch pixel budget を超えます: {entry.sample_id}"
            )
        candidate = [*batch, entry]
        cost = (
            len(candidate)
            * _ceil_to(max(item.height for item in candidate), stride)
            * _ceil_to(max(item.width for item in candidate), stride)
        )
        if batch and (len(candidate) > max_batch_size or cost > max_batch_pixels):
            batches.append(tuple(item.sample_id for item in batch))
            batch = [entry]
        else:
            batch = candidate
    if batch:
        batches.append(tuple(item.sample_id for item in batch))
    return batches


def _reject_duplicate_sample_ids(shapes: Sequence[BatchShape]) -> None:
    seen: set[str] = set()
    for shape in shapes:
        if shape.sample_id in seen:
            raise ValueError(f"sample_id が重複しています: {shape.sample_id}")
        seen.add(shape.sample_id)


def _validate_batch_inputs(
    images: Sequence[Tensor],
    valid_masks: Sequence[Tensor],
    placement_seeds: Sequence[int],
    stride: int,
) -> None:
    if not images:
        raise ValueError("空 batch は collate できません")
    if len(images) != len(valid_masks) or len(images) != len(placement_seeds):
        raise ValueError("image、mask、placement seed の件数が一致しません")
    if stride < 1:
        raise ValueError("stride は正の整数が必要です")
    channels = int(images[0].shape[0])
    device = images[0].device
    dtype = images[0].dtype
    for image, mask in zip(images, valid_masks, strict=True):
        if image.ndim != 3 or int(image.shape[0]) != channels:
            raise ValueError("batch image は同じ channel 数の CHW tensor が必要です")
        if image.device != device or image.dtype != dtype:
            raise ValueError("batch image の device と dtype は統一してください")
        if mask.dtype != torch.bool or tuple(mask.shape) != (
            1,
            int(image.shape[1]),
            int(image.shape[2]),
        ):
            raise ValueError("valid pixel mask は画像と同じ bool [1, H, W] が必要です")
        if mask.device != device:
            raise ValueError("image と valid pixel mask の device が一致しません")


def _placement(
    image: Tensor, *, height: int, width: int, seed: int, training: bool
) -> tuple[int, int]:
    spare_rows = height - int(image.shape[1])
    spare_columns = width - int(image.shape[2])
    if not training:
        return spare_rows // 2, spare_columns // 2
    generator = random.Random(seed)
    return generator.randrange(spare_rows + 1), generator.randrange(spare_columns + 1)


def _ceil_to(value: int, multiple: int) -> int:
    return ((value + multiple - 1) // multiple) * multiple


__all__ = [
    "BatchShape",
    "PaddedBatch",
    "pad_image_samples",
    "plan_pixel_budget_batches",
]
