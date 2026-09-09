"""``ml.training.TrainingData`` の塗布量推定むけ実装.

Trainer は dataset の中身を知らない。知っているのは「epoch ごとに sample ID の並びが
決まり、それを batch へ実体化できる」ことだけ。ここはその 2 つを繋ぐだけの薄い層で、
読み出しは :mod:`pcbasm.pasting.paste_volume.dataset`、前処理と詰め込みは
:mod:`pcbasm.pasting.paste_volume.batch` が担う。

``plan_epoch`` は純関数でなければならない。Trainer は checkpoint に載せた batch plan と
の厳密一致を要求するので、ここが epoch 途中の resume を支えている。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Self, override

import attrs
import torch

from ml.data.batch import BatchShape, plan_pixel_budget_batches
from ml.data.split import SplitManifest, SplitName, SplitRatios
from ml.training.data import TrainingData
from pcbasm.pasting.paste_volume.batch import PasteVolumeBatch, PasteVolumeCollator
from pcbasm.pasting.paste_volume.dataset import PasteVolumeDataset
from pcbasm.pasting.paste_volume.index import PasteVolumeSampleIndex


@attrs.frozen
class PasteVolumeTrainingConfig:
    """学習データの取り回し設定.

    ``ratios`` の既定が cell 単位の 70/15/15 なのは、収集 session が 2 本しかなく
    session を最小 group にすると test split を作れないため。session をまたいだ汎化は
    別途 leave-one-group-out で見る。

    学習の乱数種は持たない。``PasteVolumeCollator.global_seed`` が唯一の出典で、
    幾何変換・view の間引き・配置・batch の並べ替えがすべてそこから決まる。
    二重に持つと片方だけ変えたときに何が変わるのか説明できなくなる。
    """

    split_seed: int = 0
    ratios: SplitRatios = SplitRatios(0.70, 0.15, 0.15)
    max_batch_pixels: int = 8_388_608
    max_batch_size: int = 32

    def validate(self) -> str | None:
        """設定の整合を返す."""

        if error := self.ratios.validate():
            return error
        if self.max_batch_pixels < 1:
            return f"max_batch_pixels は正の値が必要です: {self.max_batch_pixels}"
        if self.max_batch_size < 1:
            return f"max_batch_size は正の値が必要です: {self.max_batch_size}"
        return None


class PasteVolumeTrainingData(TrainingData[PasteVolumeBatch]):
    """読み出し・詰め込み・split の割り当てを Trainer の契約へ繋ぐ."""

    def __init__(
        self,
        dataset: PasteVolumeDataset,
        *,
        collator: PasteVolumeCollator,
        config: PasteVolumeTrainingConfig,
        split_manifest: SplitManifest,
    ) -> None:
        self._dataset = dataset
        self._collator = collator
        self._config = config
        self._split_manifest = split_manifest

    @classmethod
    def build(
        cls,
        index: PasteVolumeSampleIndex,
        *,
        collator: PasteVolumeCollator,
        config: PasteVolumeTrainingConfig,
        split_manifest_path: Path | None = None,
    ) -> tuple[Self | None, str | None]:
        """設定を検証し、split を決めて組み立てる.

        ``augmentation`` が元画像を下限より小さくしうる設定はここで弾く。通してしまうと
        ``materialize`` の途中で sample が落ち、``plan_epoch`` の計画と食い違う。

        index と collator の ``constraints`` が一致することも要求する。index はこの制約で
        使えない cell を隔離しているので、collator 側が違う制約を使うと「拒否は index を
        作る時点で済ませる」という前提が崩れる。緩いと materialize で落ち、厳しいと母集団が
        黙って減る。

        split が空でないことは確かめ直さない。新規に作る場合は ``SplitManifest.build`` が
        ``require_test=True`` のとき各 split へ最低 1 group を割り当て、group が 3 個に
        満たなければ理由を返す。既存 manifest を読む場合は ``validate`` が group の
        取りこぼしを検出する。
        """

        if error := config.validate():
            return None, error
        if error := collator.validate():
            return None, error
        if collator.constraints != index.constraints:
            return None, (
                "index と collator の constraints が違います: "
                f"{index.constraints} と {collator.constraints}"
            )
        if error := collator.constraints.validate_augmentation(
            collator.augmentation, smallest_source_size=index.smallest_source_size
        ):
            return None, error
        manifest, error = _resolve_split(index, config=config, path=split_manifest_path)
        if manifest is None:
            return None, error
        return (
            cls(
                PasteVolumeDataset(index),
                collator=collator,
                config=config,
                split_manifest=manifest,
            ),
            None,
        )

    @property
    def index(self) -> PasteVolumeSampleIndex:
        """元になった sample index."""

        return self._dataset.index

    @property
    def collator(self) -> PasteVolumeCollator:
        """詰め込みを担う collator."""

        return self._collator

    @property
    def split_manifest(self) -> SplitManifest:
        """使っている split の割り当て."""

        return self._split_manifest

    @property
    @override
    def dataset_fingerprint(self) -> str:
        return self.index.dataset_fingerprint

    def sample_ids_for(self, split: SplitName) -> tuple[str, ...]:
        """その split に属する sample ID."""

        return self._split_manifest.sample_ids_for(split)

    @override
    def plan_epoch(
        self, *, split: SplitName, epoch: int
    ) -> tuple[tuple[str, ...], ...]:
        training = _is_training(split)
        shapes = []
        for sample_id in self.sample_ids_for(split):
            entry = self.index.entry_for(sample_id)
            shape = self._collator.preprocessed_shape(
                entry, training=training, epoch=epoch
            )
            shapes.append(
                BatchShape(
                    sample_id=sample_id,
                    height=shape.height,
                    width=shape.width,
                    view_count=entry.view_count,
                )
            )
        return plan_pixel_budget_batches(
            shapes,
            max_batch_pixels=self._config.max_batch_pixels,
            max_batch_size=self._config.max_batch_size,
            stride=self._collator.constraints.stride,
            seed=self._collator.global_seed,
            epoch=epoch,
        )

    @override
    def materialize(
        self,
        sample_ids: Sequence[str],
        *,
        split: SplitName,
        epoch: int,
        training: bool,
        device: torch.device,
    ) -> PasteVolumeBatch:
        if training != _is_training(split):
            raise ValueError(
                "training と split が食い違っています: "
                f"training={training}、split={split}"
            )
        return self._collator.collate(
            [self._dataset.sample(sample_id) for sample_id in sample_ids],
            epoch=epoch,
            training=training,
            device=device,
        )


def _is_training(split: SplitName) -> bool:
    """その split を学習として扱うか.

    ``plan_epoch`` は split しか受け取らないので、augmentation の有無を split から
    決めるほかない。``materialize`` の ``training`` がこれと食い違うと、計画時と実際の
    shape がずれる。``ml.training.loop`` は train を ``training=True``、validation を
    ``training=False`` で実体化する。
    """

    return split == "train"


def _resolve_split(
    index: PasteVolumeSampleIndex,
    *,
    config: PasteVolumeTrainingConfig,
    path: Path | None,
) -> tuple[SplitManifest | None, str | None]:
    """既存の manifest を読むか、無ければ作る."""

    groups = index.sample_groups()
    if path is not None and path.is_file():
        manifest, error = SplitManifest.load(
            path, dataset_fingerprint=index.dataset_fingerprint
        )
        if manifest is None:
            return None, error
        if error := manifest.validate(
            groups, dataset_fingerprint=index.dataset_fingerprint
        ):
            return None, error
        return manifest, None
    manifest, error = SplitManifest.build(
        groups,
        dataset_fingerprint=index.dataset_fingerprint,
        seed=config.split_seed,
        ratios=config.ratios,
        require_test=True,
    )
    if manifest is None:
        return None, error
    if path is not None:
        manifest.save(path)
    return manifest, None


__all__ = [
    "PasteVolumeTrainingConfig",
    "PasteVolumeTrainingData",
]
