"""Paste-volume学習batchとdata source contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

import torch
from torch import Tensor


@dataclass(frozen=True)
class TrainingBatch:
    """学習coreへ渡す1 batch."""

    image_6ch: Tensor
    valid_pixel_mask: Tensor
    pixel_per_mm: Tensor
    target_volume_ul: Tensor
    sample_weight: Tensor
    sample_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        batch_size = self.image_6ch.shape[0]
        if len(self.sample_ids) != batch_size:
            raise ValueError("sample_ids and tensor batch size must match")
        expected = (batch_size, 1)
        for name, tensor in (
            ("pixel_per_mm", self.pixel_per_mm),
            ("target_volume_ul", self.target_volume_ul),
            ("sample_weight", self.sample_weight),
        ):
            if tensor.shape != expected:
                raise ValueError(f"{name} must have shape [B, 1]")

    def to(self, device: torch.device) -> TrainingBatch:
        return TrainingBatch(
            image_6ch=self.image_6ch.to(device),
            valid_pixel_mask=self.valid_pixel_mask.to(device),
            pixel_per_mm=self.pixel_per_mm.to(device),
            target_volume_ul=self.target_volume_ul.to(device),
            sample_weight=self.sample_weight.to(device),
            sample_ids=self.sample_ids,
        )


class TrainingData(Protocol):
    """Deterministic batch planとtensor materializeを分離する境界."""

    def training_batch_plan(self, epoch: int) -> tuple[tuple[str, ...], ...]: ...

    def training_batch(
        self, sample_ids: tuple[str, ...], *, epoch: int
    ) -> TrainingBatch: ...

    def evaluation_batch_plan(
        self, split: Literal["validation", "test"]
    ) -> tuple[tuple[str, ...], ...]: ...

    def evaluation_batch(
        self,
        sample_ids: tuple[str, ...],
        *,
        split: Literal["validation", "test"],
    ) -> TrainingBatch: ...


__all__ = ["TrainingBatch", "TrainingData"]
