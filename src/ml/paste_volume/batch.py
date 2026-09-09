"""1 sample ずつの前処理と、batch への詰め込み.

Dataset が読んだ ``uint8`` を model が受け取る 5 次元 tensor へ変える。担うのは
view の間引き・augmentation・サイズ合わせ・padding・条件変数・loss weight。

幾何変換が collate 側にあるのは、augmentation の parameter と間引く view が
``(sample_id, epoch)`` と batch から決まるため。1 sample を読む時点では決まらない。

``[B, V, C, H, W]`` は batch 内で view 数が揃っていることを要求する。そのため
``ViewDropout`` は batch ごとに枚数を 1 つ選び、sample ごとに違う部分集合を引く。
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import attrs
import torch
from torch import Tensor

from ml.artifact.fingerprint import sha256_bytes
from ml.data.batch import MultiViewPaddedBatch, ViewDropout
from ml.data.image import (
    NO_AUGMENTATION,
    AugmentationParameters,
    AugmentationRange,
    ImageConstraints,
    ImageShape,
    PreprocessedMultiViewSample,
)
from ml.paste_volume.dataset import PasteVolumeRawSample
from ml.paste_volume.index import PasteVolumeSampleEntry

# placement seed を 64 bit へ丸める桁数（sha256 の先頭 16 桁）
_PLACEMENT_SEED_DIGITS = 16

# 乱数種の材料へ入れる役割ラベル。augmentation の種と材料が一致するのを防ぐ。
_PLACEMENT_ROLE = "placement"


@attrs.frozen(eq=False)
class PasteVolumeBatch:
    """学習 model が受け取る 1 batch.

    ``conditioning`` は ``log(有効 pixel_per_mm)`` で、sample あたり 1 本。view 軸を
    持たせると平均 pooling の view 数不変性が壊れる。
    """

    images: Tensor
    valid_pixel_mask: Tensor
    conditioning: Tensor
    target: Tensor
    sample_weight: Tensor
    sample_ids: tuple[str, ...]


@attrs.frozen
class PasteVolumeCollator:
    """1 sample ずつ前処理して batch へ詰める."""

    constraints: ImageConstraints = ImageConstraints()
    augmentation: AugmentationRange = AugmentationRange()
    view_dropout: ViewDropout = ViewDropout()
    global_seed: int = 0

    def validate(self) -> str | None:
        """設定の整合を返す."""

        if error := self.constraints.validate():
            return error
        if error := self.augmentation.validate():
            return error
        return self.view_dropout.validate()

    def parameters_for(
        self, sample_id: str, *, training: bool, epoch: int
    ) -> AugmentationParameters:
        """その sample・その epoch に当たる幾何変換を返す.

        学習時以外は恒等。大域乱数を読まないので、worker 数や中断再開で変わらない。
        """

        if not training:
            return NO_AUGMENTATION
        return self.augmentation.parameters_for(
            sample_id=sample_id, global_seed=self.global_seed, epoch=epoch
        )

    def preprocessed_shape(
        self, entry: PasteVolumeSampleEntry, *, training: bool, epoch: int
    ) -> ImageShape:
        """前処理後の高さ・幅を返す.

        ``plan_epoch`` がこの値で bucket と pixel budget を決める。``collate`` は実 decode
        から同じ関数を通すので、entry の寸法が実画像と一致する限り必ず一致する。
        """

        return ImageShape(entry.source_height, entry.source_width).preprocessed(
            constraints=self.constraints,
            parameters=self.parameters_for(
                entry.sample_id, training=training, epoch=epoch
            ),
        )

    def collate(
        self,
        samples: Sequence[PasteVolumeRawSample],
        *,
        epoch: int,
        training: bool,
        device: torch.device,
    ) -> PasteVolumeBatch:
        """1 batch を組み立てる.

        失敗はすべて呼び出し側の不変条件違反なので例外にする。設定そのものの整合は
        :meth:`PasteVolumeCollator.validate` が理由の文字列で返す。

        組み上げた tensor を自分で検算し直すことはしない。形は構築の仕方で決まり、
        その性質は collate の出力に対するテストが固定している。
        """

        if not samples:
            raise ValueError("空の batch は collate できません")
        sample_ids = tuple(sample.entry.sample_id for sample in samples)
        view_indices = self._view_indices(samples, epoch=epoch, training=training)
        images: list[Tensor] = []
        masks: list[Tensor] = []
        scales: list[float] = []
        for sample, kept in zip(samples, view_indices, strict=True):
            preprocessed = self._preprocess(
                sample, kept=kept, epoch=epoch, training=training
            )
            images.append(preprocessed.images.to(device))
            masks.append(preprocessed.valid_mask.to(device))
            scales.append(preprocessed.scale)
        padded = MultiViewPaddedBatch.pad(
            images,
            masks,
            placement_seeds=[
                _placement_seed(self.global_seed, epoch, sample_id)
                for sample_id in sample_ids
            ],
            training=training,
            stride=self.constraints.stride,
        )
        return PasteVolumeBatch(
            images=padded.images,
            valid_pixel_mask=padded.valid_pixel_masks,
            conditioning=_column(
                [
                    math.log(sample.entry.pixel_per_mm * scale)
                    for sample, scale in zip(samples, scales, strict=True)
                ],
                device,
            ),
            target=_column(
                [sample.entry.measured_volume_ul for sample in samples], device
            ),
            sample_weight=_column(
                [1.0 / sample.entry.session_sample_count for sample in samples], device
            ),
            sample_ids=sample_ids,
        )

    def _view_indices(
        self,
        samples: Sequence[PasteVolumeRawSample],
        *,
        epoch: int,
        training: bool,
    ) -> tuple[tuple[int, ...], ...]:
        available = {len(sample.view_images) for sample in samples}
        if len(available) != 1:
            raise ValueError(
                f"batch 内の view 数がそろっていません: {sorted(available)}"
            )
        view_count = next(iter(available))
        if not training:
            return tuple(tuple(range(view_count)) for _ in samples)
        indices, reason = self.view_dropout.view_indices_for(
            [sample.entry.sample_id for sample in samples],
            available_view_count=view_count,
            global_seed=self.global_seed,
            epoch=epoch,
        )
        if indices is None:
            raise ValueError(f"view を間引けません: {reason}")
        return indices

    def _preprocess(
        self,
        sample: PasteVolumeRawSample,
        *,
        kept: tuple[int, ...],
        epoch: int,
        training: bool,
    ) -> PreprocessedMultiViewSample:
        preprocessed, reason = PreprocessedMultiViewSample.preprocess(
            [list(sample.view_images[index]) for index in kept],
            constraints=self.constraints,
            parameters=self.parameters_for(
                sample.entry.sample_id, training=training, epoch=epoch
            ),
        )
        if preprocessed is None:
            raise ValueError(f"{sample.entry.sample_id} を前処理できません: {reason}")
        return preprocessed


def _column(values: Sequence[float], device: torch.device) -> Tensor:
    return torch.tensor(values, dtype=torch.float32, device=device).unsqueeze(1)


def _placement_seed(global_seed: int, epoch: int, sample_id: str) -> int:
    """余白へ置く位置を決める乱数種.

    **役割ラベルを必ず混ぜる。** ``AugmentationRange.parameters_for`` は
    ``{global_seed}:{epoch}:{sample_id}`` を sha256 に掛けた先頭 8 byte を種にするので、
    同じ材料を使うと回転角と配置位置が同じ乱数列から出て相関する。
    ``ViewDropout`` が ``view-dropout`` を挟んでいるのと同じ理由。

    split は混ぜない。位置をずらすのは学習時だけで、学習に使う split は常に 1 つなので、
    区別しても観測できる違いが生まれない。
    """

    digest = sha256_bytes(
        f"{global_seed}:{epoch}:{_PLACEMENT_ROLE}:{sample_id}".encode()
    )
    return int(digest[:_PLACEMENT_SEED_DIGITS], 16)


__all__ = [
    "PasteVolumeBatch",
    "PasteVolumeCollator",
]
