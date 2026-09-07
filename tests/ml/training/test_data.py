"""学習データ ABC と、合成 dataset が満たす resume 前提の公開契約."""

from __future__ import annotations

from collections.abc import Sequence
from typing import override

import pytest
import torch

from ml.data.split import SplitName
from ml.training.data import TrainingData
from ml.training.task import GaussianBatch
from tests.ml.support import (
    MAX_BATCH_SIZE,
    SyntheticDatasetOptions,
    SyntheticRegressionData,
)


class _IncompleteData(TrainingData[GaussianBatch]):
    """``dataset_fingerprint`` しか実装していない、契約違反の検証用 data."""

    @property
    @override
    def dataset_fingerprint(self) -> str:
        return "sha256:0"


DEVICE = torch.device("cpu")


def _data(**overrides: int) -> SyntheticRegressionData:
    return SyntheticRegressionData(SyntheticDatasetOptions(**overrides))


class TestTrainingDataContract:
    """``TrainingData`` は実装漏れを instantiate 時に弾く."""

    def test_partial_implementation_cannot_be_instantiated(self):
        with pytest.raises(TypeError) as exception:
            _IncompleteData()  # pyright: ignore[reportAbstractUsage]

        assert "abstract" in str(exception.value).lower()

    def test_synthetic_data_implements_the_abstract_methods(self):
        assert isinstance(_data(), TrainingData)


class TestDatasetFingerprint:
    """Fingerprint は内容から決まり、resume 互換性の判定に使える."""

    def test_fingerprint_is_a_sha256_reference(self):
        assert _data().dataset_fingerprint.startswith("sha256:")

    def test_same_options_give_the_same_fingerprint(self):
        assert _data().dataset_fingerprint == _data().dataset_fingerprint

    def test_different_options_give_a_different_fingerprint(self):
        assert _data().dataset_fingerprint != _data(sample_count=8).dataset_fingerprint


class TestPlanEpoch:
    """Epoch 計画は決定論的で、全 sample をちょうど 1 回並べる."""

    def test_same_epoch_gives_the_same_plan(self):
        data = _data()

        assert data.plan_epoch(split="train", epoch=0) == data.plan_epoch(
            split="train", epoch=0
        )

    def test_plan_is_stable_across_instances(self):
        assert _data().plan_epoch(split="train", epoch=1) == _data().plan_epoch(
            split="train", epoch=1
        )

    def test_different_epoch_changes_the_order(self):
        data = _data()

        first = data.plan_epoch(split="train", epoch=0)
        second = data.plan_epoch(split="train", epoch=1)

        assert first != second

    def test_every_sample_appears_exactly_once(self):
        data = _data()

        plan = data.plan_epoch(split="train", epoch=0)

        planned = [sample_id for batch in plan for sample_id in batch]
        assert sorted(planned) == sorted(data.sample_ids_for("train"))

    def test_batches_respect_the_maximum_batch_size(self):
        data = _data()

        plan = data.plan_epoch(split="train", epoch=0)

        assert plan != ()
        assert all(1 <= len(batch) <= MAX_BATCH_SIZE for batch in plan)

    def test_train_and_validation_do_not_share_samples(self):
        data = _data()

        train = set(data.sample_ids_for("train"))
        validation = set(data.sample_ids_for("validation"))

        assert train & validation == set()
        assert train != set()
        assert validation != set()

    def test_single_sample_split_gives_one_batch(self):
        data = _data(sample_count=2)

        plan = data.plan_epoch(split="train", epoch=0)

        assert [len(batch) for batch in plan] == [1]


class TestMaterialize:
    """Batch は計画された sample から決定論的に組み立てられる."""

    def test_batch_shapes_follow_the_sample_ids(self):
        data = _data()
        sample_ids: Sequence[str] = data.plan_epoch(split="train", epoch=0)[0]

        batch = data.materialize(
            sample_ids, split="train", epoch=0, training=True, device=DEVICE
        )

        assert batch.validate() is None
        assert tuple(batch.target.shape) == (len(sample_ids), 1)
        assert tuple(batch.sample_weight.shape) == (len(sample_ids), 1)
        assert int(batch.images.shape[0]) == len(sample_ids)

    def test_targets_are_positive(self):
        data = _data()
        sample_ids = data.plan_epoch(split="train", epoch=0)[0]

        batch = data.materialize(
            sample_ids, split="train", epoch=0, training=False, device=DEVICE
        )

        assert bool((batch.target > 0).all())

    def test_same_arguments_give_the_same_batch(self):
        data = _data()
        sample_ids = data.plan_epoch(split="train", epoch=0)[0]

        first = data.materialize(
            sample_ids, split="train", epoch=0, training=True, device=DEVICE
        )
        second = data.materialize(
            sample_ids, split="train", epoch=0, training=True, device=DEVICE
        )

        assert torch.equal(first.images, second.images)
        assert torch.equal(first.target, second.target)

    def test_target_is_derived_from_the_sample_identity(self):
        data = _data()
        sample_ids = data.plan_epoch(split="train", epoch=0)[0]

        batch = data.materialize(
            sample_ids, split="train", epoch=0, training=False, device=DEVICE
        )

        expected = [data.target_for(sample_id) for sample_id in sample_ids]
        assert batch.target.reshape(-1).tolist() == pytest.approx(expected)

    def test_validation_split_is_materializable(self):
        data = _data()
        split: SplitName = "validation"
        plan = data.plan_epoch(split=split, epoch=0)

        batch = data.materialize(
            plan[0], split=split, epoch=0, training=False, device=DEVICE
        )

        assert batch.validate() is None
