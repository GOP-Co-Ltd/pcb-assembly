"""Epoch 計画と batch materialize の契約.

Trainer は dataset の中身を知らない。

知っているのは「epoch ごとに sample ID の並びが決まり、それを batch へ実体化
できる」ことだけで、その境界がこの module の :class:`TrainingData` になる。

計画は epoch と split だけで決まる純関数とする。

中断した epoch の途中から、同じ batch 並びで再開できるようにするため。
"""

from __future__ import annotations

import abc
from collections.abc import Sequence

from ml.data.split import SplitName


class TrainingData[BatchT](abc.ABC):
    """Sample ID の epoch 計画と、その実体化を受け持つ境界."""

    @property
    @abc.abstractmethod
    def dataset_fingerprint(self) -> str:
        """Dataset 内容の同一性を表す ``"sha256:..."`` 形式の指紋.

        Trainer は resume 時にこの値の一致を要求する。
        """

    @abc.abstractmethod
    def plan_epoch(
        self, *, split: SplitName, epoch: int
    ) -> tuple[tuple[str, ...], ...]:
        """1 epoch 分の batch を sample ID の並びとして返す.

        同じ ``split`` と ``epoch`` なら常に同じ計画を返すこと。

        epoch 途中からの resume がこの性質を前提にしている。
        """

    @abc.abstractmethod
    def materialize(
        self,
        sample_ids: Sequence[str],
        *,
        split: SplitName,
        epoch: int,
        training: bool,
    ) -> BatchT:
        """Sample ID の並びを 1 個の batch へ実体化する."""


__all__ = [
    "TrainingData",
]
