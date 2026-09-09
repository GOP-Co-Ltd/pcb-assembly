"""可変サイズ画像の batch 計画と padding.

固定枚数で batch を作ると、最大画像に引きずられて padding と memory が増える。

そこで aspect ratio と面積で bucket 化し、padding 後の画素数を予算にして詰める。

多視点 sample は view 数も bucket 鍵に含める。

``[B, V, C, H, W]`` は batch 内で V が揃っている必要があり、view 軸の padding と
view 妥当性 mask を持ち込まずに済ませるため。

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
    view_count: int = 1

    def validate(self) -> str | None:
        """Batch 計画が意味を持つ形かを検証する.

        0 以下だと padding 後の画素数が 0 になる。

        pixel budget が効かなくなり、1 batch へ無制限に詰まる。
        """

        for name in ("height", "width", "view_count"):
            value: int = getattr(self, name)
            if value < 1:
                return f"{name} は正の整数が必要です: {self.sample_id} の {value}"
        return None


@attrs.frozen
class ViewDropout:
    """学習時に batch 単位で view を間引く設定.

    batch ごとに view 数を 1 つ選び、各 sample からその数だけ view を選ぶ。

    ``[B, V, C, H, W]`` は batch 内で V が揃っている必要があるため、sample ごとに
    別々の枚数を残す作りにはしない。

    推論は 1 view でも全 view でも通るので、間引くのは学習経路だけとする。
    """

    minimum_view_count: int = 1

    def validate(self) -> str | None:
        """間引いたあとに残す view 数の下限を検証する."""

        if self.minimum_view_count < 1:
            return f"minimum_view_count は正の整数が必要です: {self.minimum_view_count}"
        return None

    def view_indices_for(
        self,
        sample_ids: Sequence[str],
        *,
        available_view_count: int,
        global_seed: int,
        epoch: int,
    ) -> tuple[tuple[tuple[int, ...], ...] | None, str | None]:
        """Batch 内で共通の view 数を選び、sample ごとの view 番号列を返す.

        ``(global_seed, epoch, sample_ids)`` から決定論的に決める。

        大域的な乱数状態や worker 数で同じ batch の間引き方が変わらないようにする
        ため。
        """

        if error := self.validate():
            return None, error
        if available_view_count < self.minimum_view_count:
            return None, (
                "利用できる view 数が minimum_view_count を下回ります: "
                f"available_view_count={available_view_count}、"
                f"minimum_view_count={self.minimum_view_count}"
            )
        generator = random.Random(
            f"{global_seed}:{epoch}:view-dropout:{'|'.join(sample_ids)}"
        )
        count = generator.randint(self.minimum_view_count, available_view_count)
        return (
            tuple(
                tuple(sorted(generator.sample(range(available_view_count), count)))
                for _ in sample_ids
            ),
            None,
        )


@attrs.frozen(eq=False)
class PaddedBatch:
    """共通サイズへそろえた画像と、実画像の位置を示す mask.

    Tensor は要素ごとの比較になり真偽値へ落ちないので、等価性は identity で決める。
    """

    images: Tensor
    valid_pixel_masks: Tensor

    @classmethod
    def pad(
        cls,
        images: Sequence[Tensor],
        valid_masks: Sequence[Tensor],
        *,
        placement_seeds: Sequence[int],
        training: bool = False,
        stride: int = 8,
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
                image_height=int(image.shape[1]),
                image_width=int(image.shape[2]),
                height=height,
                width=width,
                seed=placement_seed,
                training=training,
            )
            rows = slice(top, top + int(image.shape[1]))
            columns = slice(left, left + int(image.shape[2]))
            padded_images[index, :, rows, columns] = image
            padded_masks[index, :, rows, columns] = mask
        return cls(images=padded_images, valid_pixel_masks=padded_masks)


@attrs.frozen(eq=False)
class MultiViewPaddedBatch:
    """View 軸を持つ画像と、view 間で共通の有効画素 mask を並べた batch.

    Tensor は要素ごとの比較になり真偽値へ落ちないので、等価性は identity で決める。
    """

    images: Tensor
    valid_pixel_masks: Tensor

    @classmethod
    def pad(
        cls,
        images: Sequence[Tensor],
        valid_masks: Sequence[Tensor],
        *,
        placement_seeds: Sequence[int],
        training: bool = False,
        stride: int = 8,
    ) -> MultiViewPaddedBatch:
        """``[V, C, H, W]`` を ``[B, V, C, H, W]`` へ stride 揃えで padding する.

        mask は view 間で共通なので ``[1, H, W]`` を受け取り、内部で view 軸へ
        broadcast する。

        配置位置は sample 内の全 view で共有する。view ごとにずらすと、view を
        またいだ位置合わせが壊れるため。
        """

        view_count = _validate_multi_view_batch_inputs(
            images, valid_masks, placement_seeds, stride
        )
        channels = int(images[0].shape[1])
        height = _ceil_to(max(int(image.shape[2]) for image in images), stride)
        width = _ceil_to(max(int(image.shape[3]) for image in images), stride)
        padded_images = torch.zeros(
            (len(images), view_count, channels, height, width),
            dtype=images[0].dtype,
            device=images[0].device,
        )
        padded_masks = torch.zeros(
            (len(images), view_count, 1, height, width),
            dtype=torch.bool,
            device=images[0].device,
        )
        for index, (image, mask, placement_seed) in enumerate(
            zip(images, valid_masks, placement_seeds, strict=True)
        ):
            top, left = _placement(
                image_height=int(image.shape[2]),
                image_width=int(image.shape[3]),
                height=height,
                width=width,
                seed=placement_seed,
                training=training,
            )
            rows = slice(top, top + int(image.shape[2]))
            columns = slice(left, left + int(image.shape[3]))
            padded_images[index, :, :, rows, columns] = image
            padded_masks[index, :, :, rows, columns] = mask
        return cls(images=padded_images, valid_pixel_masks=padded_masks)


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
    for shape in shapes:
        if error := shape.validate():
            raise ValueError(error)
    _reject_duplicate_sample_ids(shapes)

    buckets: dict[tuple[int, int, int], list[BatchShape]] = defaultdict(list)
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


def _bucket_key(shape: BatchShape) -> tuple[int, int, int]:
    aspect = round(math.log2(shape.width / shape.height) / _ASPECT_BUCKET_STEP)
    area = round(math.log2(shape.width * shape.height) / _AREA_BUCKET_STEP)
    return shape.view_count, aspect, area


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
        alone = (
            entry.view_count
            * _ceil_to(entry.height, stride)
            * _ceil_to(entry.width, stride)
        )
        if alone > max_batch_pixels:
            raise ValueError(
                f"1 sample が batch pixel budget を超えます: {entry.sample_id}"
            )
        candidate = [*batch, entry]
        # view 数を無視すると pixel budget が view 数ぶん過小評価になる
        cost = (
            sum(item.view_count for item in candidate)
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


def _validate_batch_common(
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


def _reject_mixed_placement(
    image: Tensor, mask: Tensor, *, device: torch.device, dtype: torch.dtype
) -> None:
    if image.device != device or image.dtype != dtype:
        raise ValueError("batch image の device と dtype は統一してください")
    if mask.device != device:
        raise ValueError("image と valid pixel mask の device が一致しません")


def _reject_invalid_mask(mask: Tensor, *, height: int, width: int) -> None:
    if mask.dtype != torch.bool or tuple(mask.shape) != (1, height, width):
        raise ValueError("valid pixel mask は画像と同じ bool [1, H, W] が必要です")


def _validate_batch_inputs(
    images: Sequence[Tensor],
    valid_masks: Sequence[Tensor],
    placement_seeds: Sequence[int],
    stride: int,
) -> None:
    _validate_batch_common(images, valid_masks, placement_seeds, stride)
    channels = int(images[0].shape[0])
    device = images[0].device
    dtype = images[0].dtype
    for image, mask in zip(images, valid_masks, strict=True):
        if image.ndim != 3 or int(image.shape[0]) != channels:
            raise ValueError("batch image は同じ channel 数の CHW tensor が必要です")
        _reject_mixed_placement(image, mask, device=device, dtype=dtype)
        _reject_invalid_mask(
            mask, height=int(image.shape[1]), width=int(image.shape[2])
        )


def _validate_multi_view_batch_inputs(
    images: Sequence[Tensor],
    valid_masks: Sequence[Tensor],
    placement_seeds: Sequence[int],
    stride: int,
) -> int:
    """多視点 batch の入力を検証し、batch 共通の view 数を返す."""

    _validate_batch_common(images, valid_masks, placement_seeds, stride)
    if images[0].ndim != 4:
        raise ValueError(
            f"batch image は [V, C, H, W] が必要です: {tuple(images[0].shape)}"
        )
    view_count = int(images[0].shape[0])
    channels = int(images[0].shape[1])
    device = images[0].device
    dtype = images[0].dtype
    for image, mask in zip(images, valid_masks, strict=True):
        if image.ndim != 4:
            raise ValueError(
                f"batch image は [V, C, H, W] が必要です: {tuple(image.shape)}"
            )
        if int(image.shape[0]) != view_count:
            raise ValueError(
                "batch 内の view 数が不揃いです: "
                f"{view_count} と {int(image.shape[0])}"
            )
        if int(image.shape[1]) != channels:
            raise ValueError(
                "batch image は同じ channel 数が必要です: "
                f"{channels} と {int(image.shape[1])}"
            )
        _reject_mixed_placement(image, mask, device=device, dtype=dtype)
        _reject_invalid_mask(
            mask, height=int(image.shape[2]), width=int(image.shape[3])
        )
    return view_count


def _placement(
    *,
    image_height: int,
    image_width: int,
    height: int,
    width: int,
    seed: int,
    training: bool,
) -> tuple[int, int]:
    spare_rows = height - image_height
    spare_columns = width - image_width
    if not training:
        return spare_rows // 2, spare_columns // 2
    generator = random.Random(seed)
    return generator.randrange(spare_rows + 1), generator.randrange(spare_columns + 1)


def _ceil_to(value: int, multiple: int) -> int:
    return ((value + multiple - 1) // multiple) * multiple


__all__ = [
    "BatchShape",
    "MultiViewPaddedBatch",
    "PaddedBatch",
    "ViewDropout",
    "plan_pixel_budget_batches",
]
