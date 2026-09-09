"""``ml.training.TrainingData`` 実装の公開契約.

Trainer は dataset の中身を知らない。知っているのは「epoch ごとに sample ID の並びが 決まり、それを
batch へ実体化できる」ことだけ。その 2 つの契約をここで固定する。
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
import torch

from ml.data.image import AugmentationRange, ImageConstraints, ImageShape
from ml.data.split import SplitManifest, SplitRatios
from ml.paste_volume.batch import PasteVolumeCollator
from ml.paste_volume.index import PasteVolumeSampleIndex
from ml.paste_volume.task import (
    PasteVolumeTrainingConfig,
    PasteVolumeTrainingData,
)
from tests.ml.paste_volume.helpers import (
    CROP_SIZE_PX,
    VIEW_COUNT,
    SyntheticCell,
    skip_if_no_real_sessions,
    write_session,
)

CONSTRAINTS = ImageConstraints()
DEVICE = torch.device("cpu")

# split を 3 つとも埋めるには group が 3 個以上要る
CELLS = tuple(
    SyntheticCell(
        index=number,
        commanded_volume_ul=None if number == 4 else 0.05 * number,
        x_mm=0.5 + 2.2 * number,
    )
    for number in range(1, 13)
)


def _index(
    tmp_path: Path,
    *,
    sessions: int = 1,
    cells: tuple[SyntheticCell, ...] = CELLS,
    machine: str = "m",
) -> PasteVolumeSampleIndex:
    roots = [
        write_session(
            tmp_path / f"session-{number}", cells=cells, machine_id=f"{machine}{number}"
        )
        for number in range(sessions)
    ]
    index, reason = PasteVolumeSampleIndex.from_roots(roots, constraints=CONSTRAINTS)
    assert index is not None, reason
    return index


def _data(
    tmp_path: Path,
    *,
    index: PasteVolumeSampleIndex | None = None,
    collator: PasteVolumeCollator | None = None,
    **overrides: object,
) -> PasteVolumeTrainingData:
    data, reason = PasteVolumeTrainingData.build(
        index if index is not None else _index(tmp_path),
        collator=collator or PasteVolumeCollator(global_seed=5),
        config=PasteVolumeTrainingConfig(**overrides),  # type: ignore[arg-type]
    )
    assert data is not None, reason
    return data


def _area_bucket(shape: ImageShape) -> int:
    """``plan_pixel_budget_batches`` が bucket を切る単位（log2 面積の 1.0 刻み）."""

    return round(math.log2(shape.height * shape.width))


class TestBuild:
    """組み立てと設定の検証."""

    def test_splits_every_sample_into_exactly_one_split(self, tmp_path: Path):
        data = _data(tmp_path)

        assigned = [
            sample_id
            for split in ("train", "validation", "test")
            for sample_id in data.sample_ids_for(split)  # type: ignore[arg-type]
        ]

        assert sorted(assigned) == sorted(
            entry.sample_id for entry in data.index.entries
        )
        assert len(set(assigned)) == len(assigned)

    def test_never_splits_one_physical_cell(self, tmp_path: Path):
        """同じ座標の cell は必ず同じ split.

        銅板の背景テクスチャが train と test へ分かれて漏れるのを防ぐ。
        """

        data = _data(tmp_path)

        assert (
            data.split_manifest.validate(
                data.index.sample_groups(), dataset_fingerprint=data.dataset_fingerprint
            )
            is None
        )

    def test_exposes_the_dataset_fingerprint_of_the_index(self, tmp_path: Path):
        data = _data(tmp_path)

        assert data.dataset_fingerprint == data.index.dataset_fingerprint

    def test_reports_an_augmentation_range_the_images_cannot_take(self, tmp_path: Path):
        """縮小しすぎて下限を割る設定を build で弾く.

        ここで弾けば、どの epoch でも前処理がサイズを理由に sample を落とすことは
        起こり得なくなる。materialize の途中で落ちると plan_epoch の計画と食い違う。
        """

        data, reason = PasteVolumeTrainingData.build(
            _index(tmp_path),
            collator=PasteVolumeCollator(
                augmentation=AugmentationRange(minimum_scale=0.01, maximum_scale=1.0)
            ),
            config=PasteVolumeTrainingConfig(),
        )

        assert data is None
        assert reason is not None
        assert "minimum_size" in reason or "minimum_scale" in reason

    def test_reports_a_dataset_with_too_few_cell_groups(self, tmp_path: Path):
        """物理 cell が 3 個に満たないと split を作れない.

        group が不可分なので、train / validation / test を埋めるには最低 3 個要る。
        比率をどう振っても各 split へ最低 1 group が入るため、空の split は作られない。
        """

        data, reason = PasteVolumeTrainingData.build(
            _index(tmp_path, cells=CELLS[:2]),
            collator=PasteVolumeCollator(),
            config=PasteVolumeTrainingConfig(),
        )

        assert data is None
        assert reason is not None
        assert "group" in reason

    def test_reports_a_ratio_that_is_not_usable(self, tmp_path: Path):
        data, reason = PasteVolumeTrainingData.build(
            _index(tmp_path),
            collator=PasteVolumeCollator(),
            config=PasteVolumeTrainingConfig(ratios=SplitRatios(0.9, 0.9, 0.9)),
        )

        assert data is None
        assert reason is not None

    def test_reports_a_collator_that_cannot_keep_any_view(self, tmp_path: Path):
        """詰め込み設定そのものの整合も build で見る.

        view を 1 枚も残さない設定は augmentation の範囲検証では捕まらない。
        """

        from ml.data.batch import ViewDropout

        data, reason = PasteVolumeTrainingData.build(
            _index(tmp_path),
            collator=PasteVolumeCollator(
                view_dropout=ViewDropout(minimum_view_count=0)
            ),
            config=PasteVolumeTrainingConfig(),
        )

        assert data is None
        assert reason is not None

    def test_reports_a_batch_size_that_cannot_hold_a_sample(self, tmp_path: Path):
        """学習データ設定そのものの整合も build で見る.

        batch size は split の生成にも augmentation にも関わらないので、ここで見なければ
        plan_epoch が呼ばれるまで気づけない。
        """

        data, reason = PasteVolumeTrainingData.build(
            _index(tmp_path),
            collator=PasteVolumeCollator(),
            config=PasteVolumeTrainingConfig(max_batch_size=0),
        )

        assert data is None
        assert reason is not None
        assert "max_batch_size" in reason

    def test_keeps_both_sessions_of_one_cell_in_the_same_split(self, tmp_path: Path):
        """同じ物理 cell の複数 session ぶんが同じ split に入る.

        1 session だけだと cell と sample が 1 対 1 になり、sample 単位で分けても
        差が出ない。銅板の背景が漏れるのはむしろ複数 session のときなので、そこで見る。
        """

        index = _index(tmp_path, sessions=2)
        data = _data(tmp_path, index=index)

        by_cell: dict[str, set[str]] = {}
        for split in ("train", "validation", "test"):
            for sample_id in data.sample_ids_for(split):  # type: ignore[arg-type]
                cell = index.entry_for(sample_id).cell_key
                by_cell.setdefault(cell, set()).add(split)

        assert len(index.entries) == 2 * len(CELLS)
        assert all(len(splits) == 1 for splits in by_cell.values())

    def test_reports_constraints_that_differ_from_the_index(self, tmp_path: Path):
        """読み出し側と詰め込み側で制約が違えば弾く.

        index はその制約で使えない cell を隔離している。collator が違う制約を使うと、 緩ければ
        materialize で落ち、厳しければ母集団が黙って減る。
        """

        import attrs

        data, reason = PasteVolumeTrainingData.build(
            _index(tmp_path),
            collator=PasteVolumeCollator(
                constraints=attrs.evolve(CONSTRAINTS, maximum_size=256)
            ),
            config=PasteVolumeTrainingConfig(),
        )

        assert data is None
        assert reason is not None
        assert "constraints" in reason

    def test_rejects_a_manifest_that_splits_a_physical_cell(self, tmp_path: Path):
        """内容が一致していても group を割っている manifest を弾く.

        sample 単位で作った manifest は fingerprint 検査を通ってしまう。cell が split
        をまたいでいないかは別に確かめる必要がある。
        """

        path = tmp_path / "split.json"
        index = _index(tmp_path, sessions=2)
        manifest, _ = SplitManifest.build(
            {sample_id: sample_id for sample_id in index.sample_groups()},
            dataset_fingerprint=index.dataset_fingerprint,
            seed=0,
            ratios=SplitRatios(0.7, 0.15, 0.15),
            require_test=True,
        )
        assert manifest is not None
        manifest.save(path)

        data, reason = PasteVolumeTrainingData.build(
            index,
            collator=PasteVolumeCollator(),
            config=PasteVolumeTrainingConfig(),
            split_manifest_path=path,
        )

        assert data is None
        assert reason is not None

    def test_reuses_a_saved_split_manifest(self, tmp_path: Path):
        """既にある manifest を読み直し、作り直さない."""

        path = tmp_path / "split.json"
        first = PasteVolumeTrainingData.build(
            _index(tmp_path),
            collator=PasteVolumeCollator(),
            config=PasteVolumeTrainingConfig(),
            split_manifest_path=path,
        )[0]
        assert first is not None

        second, reason = PasteVolumeTrainingData.build(
            _index(tmp_path),
            collator=PasteVolumeCollator(),
            config=PasteVolumeTrainingConfig(split_seed=999),
            split_manifest_path=path,
        )

        assert second is not None, reason
        assert second.sample_ids_for("train") == first.sample_ids_for("train")

    def test_rejects_a_manifest_built_for_another_dataset(self, tmp_path: Path):
        path = tmp_path / "split.json"
        other = _index(tmp_path / "other", machine="other")
        manifest, _ = SplitManifest.build(
            other.sample_groups(),
            dataset_fingerprint=other.dataset_fingerprint,
            seed=0,
            ratios=SplitRatios(0.7, 0.15, 0.15),
            require_test=True,
        )
        assert manifest is not None
        manifest.save(path)

        data, reason = PasteVolumeTrainingData.build(
            _index(tmp_path / "mine"),
            collator=PasteVolumeCollator(),
            config=PasteVolumeTrainingConfig(),
            split_manifest_path=path,
        )

        assert data is None
        assert reason is not None


class TestPlanEpoch:
    """毎 epoch の計画が純関数であること."""

    def test_is_stable_for_the_same_split_and_epoch(self, tmp_path: Path):
        """同じ引数なら常に同じ計画.

        Trainer は checkpoint に載せた batch plan との厳密一致を要求する。
        """

        data = _data(tmp_path)

        assert data.plan_epoch(split="train", epoch=3) == data.plan_epoch(
            split="train", epoch=3
        )

    def test_does_not_read_the_global_random_state(self, tmp_path: Path):
        data = _data(tmp_path)

        before = data.plan_epoch(split="train", epoch=1)
        torch.rand(23)

        assert data.plan_epoch(split="train", epoch=1) == before

    def test_changes_between_epochs(self, tmp_path: Path):
        data = _data(tmp_path)

        plans = {data.plan_epoch(split="train", epoch=epoch) for epoch in range(8)}

        assert len(plans) > 1

    def test_covers_the_split_exactly_once(self, tmp_path: Path):
        data = _data(tmp_path)

        planned = [
            sample_id
            for batch in data.plan_epoch(split="train", epoch=0)
            for sample_id in batch
        ]

        assert sorted(planned) == sorted(data.sample_ids_for("train"))

    def test_respects_the_batch_size_limit(self, tmp_path: Path):
        data = _data(tmp_path, max_batch_size=2)

        for batch in data.plan_epoch(split="train", epoch=0):
            assert 1 <= len(batch) <= 2

    def test_groups_a_batch_by_the_augmented_size(self, tmp_path: Path):
        """同じ batch の中身が変換後の大きさで揃う.

        計画を augmentation 前の寸法で立てると、実際には 26 px と 106 px の sample が 同じ
        batch へ入る。padding が batch 内 max へ合わせるので落ちず、pixel budget が 意味を失う。
        """

        data = _data(tmp_path)

        for epoch in range(3):
            for batch in data.plan_epoch(split="train", epoch=epoch):
                buckets = {
                    _area_bucket(
                        data.collator.preprocessed_shape(
                            data.index.entry_for(sample_id), training=True, epoch=epoch
                        )
                    )
                    for sample_id in batch
                }

                assert len(buckets) == 1

    def test_shuffles_the_evaluation_plan_between_epochs(self, tmp_path: Path):
        """評価 split の計画も epoch で変わる.

        評価は augmentation を掛けないので寸法は毎 epoch 同じ。計画が変わるのは 並べ替えの種に epoch
        を混ぜているからで、そこだけを見る観測点。
        """

        data = _data(tmp_path, index=_index(tmp_path, sessions=3), max_batch_size=2)

        plans = {data.plan_epoch(split="validation", epoch=epoch) for epoch in range(8)}

        assert len(plans) > 1

    def test_counts_every_view_against_the_pixel_budget(self, tmp_path: Path):
        """画素の上限は view 数ぶん掛かる.

        view 数を数えないと 5 倍の画素を 1 sample 分として見積もり、batch が膨らむ。 幾何
        augmentation を止めて、効いているのが view 数だけになるようにする。
        """

        still = PasteVolumeCollator(
            augmentation=AugmentationRange(
                rotation_enabled=False, minimum_scale=1.0, maximum_scale=1.0
            )
        )
        data = _data(tmp_path, collator=still, max_batch_pixels=40_000)

        for batch in data.plan_epoch(split="train", epoch=0):
            assert len(batch) <= 2


class TestMaterialize:
    """計画した sample を batch へ実体化すること."""

    def test_builds_a_batch_for_the_planned_sample_ids(self, tmp_path: Path):
        data = _data(tmp_path)
        planned = data.plan_epoch(split="train", epoch=0)[0]

        batch = data.materialize(
            planned, split="train", epoch=0, training=True, device=DEVICE
        )

        assert batch.sample_ids == tuple(planned)
        assert batch.images.shape[0] == len(planned)
        assert batch.images.shape[2] == 6

    def test_keeps_every_view_when_evaluating(self, tmp_path: Path):
        data = _data(tmp_path)
        planned = data.plan_epoch(split="validation", epoch=0)[0]

        batch = data.materialize(
            planned, split="validation", epoch=0, training=False, device=DEVICE
        )

        assert batch.images.shape[1] == VIEW_COUNT
        assert batch.images.shape[3:] == (CROP_SIZE_PX + 3, CROP_SIZE_PX + 3)

    def test_rejects_a_training_flag_that_contradicts_the_split(self, tmp_path: Path):
        """``training`` と split の食い違いを落とす.

        plan_epoch は split から augmentation の有無を決めて shape を求める。
        materialize が違う判断をすると、計画時と実際の shape がずれる。padding が batch 内 max
        へ合わせるので落ちず、黙って壊れるのがいちばん悪い。
        """

        data = _data(tmp_path)
        planned = data.plan_epoch(split="validation", epoch=0)[0]

        with pytest.raises(ValueError, match="training"):
            data.materialize(
                planned, split="validation", epoch=0, training=True, device=DEVICE
            )

    def test_the_planned_shape_bounds_the_materialized_batch(self, tmp_path: Path):
        """計画した shape と実際の shape が stride の中で一致する.

        ずれると bucket と pixel budget が意味を失う。
        """

        data = _data(tmp_path)
        planned = data.plan_epoch(split="train", epoch=2)[0]

        batch = data.materialize(
            planned, split="train", epoch=2, training=True, device=DEVICE
        )

        for row, sample_id in enumerate(planned):
            planned_shape = data.collator.preprocessed_shape(
                data.index.entry_for(sample_id), training=True, epoch=2
            )
            mask = batch.valid_pixel_mask[row, 0, 0]

            # 有効画素の外接矩形が、その sample の前処理後の寸法そのもの。padding 後の
            # canvas と比べると、planned と actual が同じ stride 窓に入る限り一致して
            # しまい、最大 stride-1 px のずれを見逃す
            assert int(mask.any(dim=1).sum()) == planned_shape.height
            assert int(mask.any(dim=0).sum()) == planned_shape.width

        stride = CONSTRAINTS.stride
        assert batch.images.shape[3] % stride == 0
        assert batch.images.shape[4] % stride == 0


class TestRealSessions:
    """実収集 session を通した確認（無ければ skip）."""

    @skip_if_no_real_sessions
    def test_builds_and_materializes_from_the_collected_sessions(self):
        from tests.ml.paste_volume.helpers import PASTE_VOLUME_DATASET_DIR

        index, reason = PasteVolumeSampleIndex.from_roots(
            [PASTE_VOLUME_DATASET_DIR], constraints=CONSTRAINTS
        )
        assert index is not None, reason
        assert not index.rejections

        data, reason = PasteVolumeTrainingData.build(
            index,
            collator=PasteVolumeCollator(),
            config=PasteVolumeTrainingConfig(),
        )
        assert data is not None, reason

        planned = data.plan_epoch(split="train", epoch=0)[0]
        batch = data.materialize(
            planned, split="train", epoch=0, training=True, device=DEVICE
        )

        assert batch.images.ndim == 5
        assert batch.images.shape[2] == 6
        assert batch.target.shape == (len(planned), 1)
