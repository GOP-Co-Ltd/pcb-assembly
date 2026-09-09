"""学習 batch を組み立てる層の公開契約.

collator は view の間引き・augmentation・サイズ合わせ・padding・conditioning・weight を
まとめて担う。Dataset が読んだ ``uint8`` を、model が受け取る 5 次元 tensor へ変える。
"""

from __future__ import annotations

import math
import random
from pathlib import Path

import attrs
import pytest
import torch

from ml.data.batch import ViewDropout
from ml.data.image import AugmentationRange, ImageConstraints
from ml.paste_volume.batch import PasteVolumeBatch, PasteVolumeCollator
from ml.paste_volume.dataset import PasteVolumeDataset, PasteVolumeRawSample
from ml.paste_volume.index import PasteVolumeSampleIndex
from tests.ml.paste_volume.helpers import (
    CROP_SIZE_PX,
    MEASURED_RATIO,
    PIXEL_PER_MM,
    VIEW_COUNT,
    SyntheticCell,
    write_session,
)
from tests.ml.test_seed_roles import derived_seed, sample_scoped_material

CONSTRAINTS = ImageConstraints()
CHANNELS = 6
DEVICE = torch.device("cpu")

# 全 collator が使う大域種。配置の種の材料に入るので、期待値の組み立てと共有する
GLOBAL_SEED = 7

# 配置の材料へ挟む役割ラベル。``ml.paste_volume.batch._PLACEMENT_ROLE`` と同じ値を、
# private を import せずに test 側で持つ
PLACEMENT_ROLE = "placement"

# 回転角を 8 帯へ畳む幅。配置との相関を見る粒度
ROTATION_SECTOR_DEGREES = 45

# 回転帯 x 配置位置の理論上限。8 帯 x（56 - 53 + 1）位置
ROTATION_SECTORS = 8
PLACEMENT_OFFSETS = 4

CELLS = (
    SyntheticCell(index=1, commanded_volume_ul=0.10, x_mm=0.5),
    SyntheticCell(index=2, commanded_volume_ul=0.30, x_mm=2.7),
    SyntheticCell(index=3, commanded_volume_ul=None, x_mm=4.9),
)


def _dataset(tmp_path: Path, **overrides: object) -> PasteVolumeDataset:
    index, reason = PasteVolumeSampleIndex.from_roots(
        [write_session(tmp_path / "session", cells=CELLS, **overrides)],  # type: ignore[arg-type]
        constraints=CONSTRAINTS,
    )
    assert index is not None, reason
    return PasteVolumeDataset(index)


def _samples(dataset: PasteVolumeDataset) -> tuple[PasteVolumeRawSample, ...]:
    return tuple(dataset.sample(entry.sample_id) for entry in dataset.index.entries)


STILL = AugmentationRange(rotation_enabled=False, minimum_scale=1.0, maximum_scale=1.0)


def _mask_offset(mask: torch.Tensor) -> tuple[int, int]:
    """有効画素の左上位置を返す。padding のどこへ置かれたかの観測点."""

    rows = torch.nonzero(mask.any(dim=1)).flatten()
    columns = torch.nonzero(mask.any(dim=0)).flatten()
    return int(rows[0]), int(columns[0])


def _offsets_of(batch: PasteVolumeBatch) -> tuple[tuple[int, int], ...]:
    """Batch の各 sample の有効画素左上位置。最初の view で見る."""

    return tuple(
        _mask_offset(batch.valid_pixel_mask[row, 0, 0])
        for row in range(batch.images.shape[0])
    )


def _expected_offset(
    batch: PasteVolumeBatch, role: str | None, sample_id: str, *, epoch: int
) -> tuple[int, int]:
    """役割ラベル ``role`` の材料から、余白へ置く位置を組み直す.

    余白は batch の実寸から取る。``PaddedBatch.pad`` は行を先に、列を後に引く。
    """

    spare_rows = int(batch.images.shape[-2]) - CROP_SIZE_PX
    spare_columns = int(batch.images.shape[-1]) - CROP_SIZE_PX
    generator = random.Random(
        derived_seed(
            sample_scoped_material(
                role, global_seed=GLOBAL_SEED, epoch=epoch, sample_id=sample_id
            )
        )
    )
    return generator.randrange(spare_rows + 1), generator.randrange(spare_columns + 1)


def _valid_region(view: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
    """余白を落として有効画素だけの ``[C, H, W]`` を返す.

    batch 内の最大寸法を stride の倍数へ切り上げるので、53 px の画像は 56 px へ padding
    される。境界を含めたまま定数 channel を見ると padding のぶん変化して見える。
    """

    rows = torch.nonzero(mask.any(dim=1)).flatten()
    columns = torch.nonzero(mask.any(dim=0)).flatten()
    return view[:, rows[0] : rows[-1] + 1, columns[0] : columns[-1] + 1]


def _collator(**overrides: object) -> PasteVolumeCollator:
    return attrs.evolve(PasteVolumeCollator(global_seed=GLOBAL_SEED), **overrides)  # type: ignore[arg-type]


class TestCollatorValidation:
    """設定の整合を理由つきで返すこと."""

    def test_accepts_the_default_configuration(self):
        assert _collator().validate() is None

    def test_reports_an_augmentation_range_that_is_not_usable(self):
        collator = _collator(
            augmentation=AugmentationRange(minimum_scale=2.0, maximum_scale=0.5)
        )

        reason = collator.validate()

        assert reason is not None
        assert "scale" in reason

    def test_reports_view_dropout_that_keeps_no_view(self):
        collator = _collator(view_dropout=ViewDropout(minimum_view_count=0))

        assert collator.validate() is not None


class TestCollatedShapes:
    """学習 model が受け取る tensor の形."""

    def test_builds_the_five_dimensional_batch(self, tmp_path: Path):
        dataset = _dataset(tmp_path)
        batch = _collator().collate(
            _samples(dataset),
            epoch=0,
            training=False,
            device=DEVICE,
        )

        assert batch.images.ndim == 5
        assert batch.images.shape[:3] == (len(CELLS), VIEW_COUNT, CHANNELS)
        assert batch.valid_pixel_mask.shape[:3] == (len(CELLS), VIEW_COUNT, 1)
        assert batch.valid_pixel_mask.dtype == torch.bool
        assert batch.images.shape[3:] == batch.valid_pixel_mask.shape[3:]

    def test_targets_and_weights_are_column_vectors(self, tmp_path: Path):
        """損失関数は 4 つの tensor が同じ shape であることを要求する."""

        dataset = _dataset(tmp_path)
        batch = _collator().collate(
            _samples(dataset),
            epoch=0,
            training=False,
            device=DEVICE,
        )

        assert batch.target.shape == (len(CELLS), 1)
        assert batch.conditioning.shape == (len(CELLS), 1)
        assert batch.sample_weight.shape == (len(CELLS), 1)
        assert batch.target.dtype == torch.float32

    def test_keeps_the_source_size_when_augmentation_is_off(self, tmp_path: Path):
        dataset = _dataset(tmp_path)
        batch = _collator().collate(
            _samples(dataset),
            epoch=0,
            training=False,
            device=DEVICE,
        )

        assert batch.images.shape[3:] == (CROP_SIZE_PX + 3, CROP_SIZE_PX + 3)

    def test_reports_the_sample_ids_in_order(self, tmp_path: Path):
        dataset = _dataset(tmp_path)
        samples = _samples(dataset)

        batch = _collator().collate(samples, epoch=0, training=False, device=DEVICE)

        assert batch.sample_ids == tuple(sample.entry.sample_id for sample in samples)


class TestChannelLayout:
    """チャネルの並びが pre RGB → post RGB であること."""

    def test_places_the_pre_image_in_the_first_three_channels(self, tmp_path: Path):
        """0-2 が塗布前、3-5 が塗布後.

        合成画像は pre の R だけが水平に、post の B だけが垂直に変化する。標準化は全 channel 共通の 1
        組の統計で行うので、定数 channel は定数のまま残る。
        """

        dataset = _dataset(tmp_path)
        batch = _collator().collate(
            _samples(dataset),
            epoch=0,
            training=False,
            device=DEVICE,
        )
        view = _valid_region(batch.images[0, 0], batch.valid_pixel_mask[0, 0, 0])

        # pre は R だけが列方向に変化し、G と B は定数
        assert not bool(torch.allclose(view[0][:, 0], view[0][:, 1]))
        assert bool(torch.allclose(view[1][:, 0], view[1][:, 1]))
        assert bool(torch.allclose(view[2][:, 0], view[2][:, 1]))
        # post は B だけが行方向に変化し、R と G は定数
        assert bool(torch.allclose(view[3][:, 0], view[3][:, 1]))
        assert bool(torch.allclose(view[4][:, 0], view[4][:, 1]))
        assert not bool(torch.allclose(view[5][0], view[5][1]))


class TestConditioning:
    """解像度の条件変数."""

    def test_is_the_log_of_the_collection_resolution_without_augmentation(
        self, tmp_path: Path
    ):
        dataset = _dataset(tmp_path)
        batch = _collator().collate(
            _samples(dataset),
            epoch=0,
            training=False,
            device=DEVICE,
        )

        assert batch.conditioning.allclose(
            torch.full((len(CELLS), 1), math.log(PIXEL_PER_MM)), atol=1e-5
        )

    def test_follows_the_scale_applied_by_augmentation(self, tmp_path: Path):
        """拡大縮小したぶん ``pixel_per_mm`` も動く.

        見かけの大きさが体積の主要な手がかりなので、scale を掛けたのに解像度を据え置くと、model
        は同じ体積を違う値として学ぶ。
        """

        dataset = _dataset(tmp_path)
        samples = _samples(dataset)
        collator = _collator()

        batch = collator.collate(samples, epoch=0, training=True, device=DEVICE)

        for row, sample in enumerate(samples):
            parameters = collator.parameters_for(
                sample.entry.sample_id, training=True, epoch=0
            )
            expected = math.log(PIXEL_PER_MM * parameters.scale)
            assert abs(float(batch.conditioning[row, 0]) - expected) < 1e-4

    def test_has_no_view_axis(self, tmp_path: Path):
        """条件変数は sample あたり 1 本.

        view 数を混ぜると平均 pooling の view 数不変性が壊れる。
        """

        dataset = _dataset(tmp_path)
        batch = _collator().collate(
            _samples(dataset),
            epoch=0,
            training=False,
            device=DEVICE,
        )

        assert batch.conditioning.ndim == 2


class TestLabelAndWeight:
    """教師値と loss weight."""

    def test_carries_the_measured_volume_including_the_exact_zero(self, tmp_path: Path):
        dataset = _dataset(tmp_path)
        samples = _samples(dataset)

        batch = _collator().collate(samples, epoch=0, training=False, device=DEVICE)

        # blank の 0 だけは厳密。他は float32 の丸めを許す
        assert float(batch.target[2, 0]) == 0.0
        assert batch.target[:, 0].tolist() == pytest.approx(
            [0.10 * MEASURED_RATIO, 0.30 * MEASURED_RATIO, 0.0], abs=1e-6
        )

    def test_weights_each_session_by_its_own_sample_count(self, tmp_path: Path):
        """重みは 1/N_session。多視点は 1 sample なので view 数では割らない."""

        dataset = _dataset(tmp_path)
        batch = _collator().collate(
            _samples(dataset),
            epoch=0,
            training=False,
            device=DEVICE,
        )

        assert batch.sample_weight.allclose(
            torch.full((len(CELLS), 1), 1.0 / len(CELLS))
        )

    def test_a_larger_session_gets_a_smaller_per_sample_weight(self, tmp_path: Path):
        big = write_session(
            tmp_path / "big",
            cells=(*CELLS, SyntheticCell(index=4, commanded_volume_ul=0.2, x_mm=7.1)),
        )
        index, reason = PasteVolumeSampleIndex.from_roots(
            [big], constraints=CONSTRAINTS
        )
        assert index is not None, reason
        dataset = PasteVolumeDataset(index)

        batch = _collator().collate(
            _samples(dataset),
            epoch=0,
            training=False,
            device=DEVICE,
        )

        assert batch.sample_weight.allclose(torch.full((4, 1), 0.25))


class TestAugmentation:
    """幾何変換が学習時だけ効くこと."""

    def test_is_disabled_outside_training(self, tmp_path: Path):
        dataset = _dataset(tmp_path)
        collator = _collator()

        for sample in _samples(dataset):
            parameters = collator.parameters_for(
                sample.entry.sample_id, training=False, epoch=3
            )

            assert parameters.rotation_degrees == 0.0
            assert parameters.scale == 1.0

    def test_changes_with_the_epoch_during_training(self, tmp_path: Path):
        dataset = _dataset(tmp_path)
        collator = _collator()
        sample_id = dataset.index.entries[0].sample_id

        first = collator.parameters_for(sample_id, training=True, epoch=0)
        second = collator.parameters_for(sample_id, training=True, epoch=1)

        assert first != second

    def test_does_not_read_the_global_random_state(self, tmp_path: Path):
        """大域乱数を挟んでも同じ parameter になる.

        worker 数や中断再開で同じ sample の変換が変わらないことの観測点。
        """

        dataset = _dataset(tmp_path)
        collator = _collator()
        sample_id = dataset.index.entries[0].sample_id

        before = collator.parameters_for(sample_id, training=True, epoch=2)
        torch.rand(17)
        after = collator.parameters_for(sample_id, training=True, epoch=2)

        assert before == after

    def test_the_planned_shape_matches_what_collate_produces(self, tmp_path: Path):
        """``preprocessed_shape`` が実 collate の結果と一致する.

        plan_epoch はこの値で bucket と pixel budget を決める。ずれても padding が batch
        内 max へ合わせるので落ちず、黙って壊れる。
        """

        dataset = _dataset(tmp_path)
        collator = _collator()

        for entry in dataset.index.entries:
            shape = collator.preprocessed_shape(entry, training=True, epoch=5)
            batch = collator.collate(
                (dataset.sample(entry.sample_id),),
                epoch=5,
                training=True,
                device=DEVICE,
            )

            assert batch.images.shape[3] >= shape.height
            assert batch.images.shape[4] >= shape.width
            assert batch.images.shape[3] - shape.height < CONSTRAINTS.stride
            assert batch.images.shape[4] - shape.width < CONSTRAINTS.stride


class TestViewDropout:
    """学習時だけ view を間引くこと."""

    def test_keeps_every_view_outside_training(self, tmp_path: Path):
        dataset = _dataset(tmp_path)

        batch = _collator().collate(
            _samples(dataset),
            epoch=0,
            training=False,
            device=DEVICE,
        )

        assert batch.images.shape[1] == VIEW_COUNT

    def test_thins_the_views_during_training(self, tmp_path: Path):
        """どこかの epoch で view 数が減る.

        毎 epoch 必ず減るわけではないので、複数 epoch を見て 1 度でも減れば足りる。
        """

        dataset = _dataset(tmp_path)
        samples = _samples(dataset)
        collator = _collator()

        counts = {
            collator.collate(
                samples, epoch=epoch, training=True, device=DEVICE
            ).images.shape[1]
            for epoch in range(12)
        }

        assert min(counts) < VIEW_COUNT
        assert min(counts) >= 1

    def test_keeps_the_view_count_uniform_within_a_batch(self, tmp_path: Path):
        """同じ batch の中で V が揃うこと.

        ``[B, V, C, H, W]`` は V の一致を要求する。sample ごとに枚数を変えられない。
        """

        dataset = _dataset(tmp_path)
        samples = _samples(dataset)
        collator = _collator()

        for epoch in range(12):
            batch = collator.collate(samples, epoch=epoch, training=True, device=DEVICE)

            assert batch.images.shape[0] == len(samples)
            assert batch.valid_pixel_mask.shape[1] == batch.images.shape[1]

    def test_is_deterministic_for_the_same_batch_and_epoch(self, tmp_path: Path):
        dataset = _dataset(tmp_path)
        samples = _samples(dataset)
        collator = _collator()

        first = collator.collate(samples, epoch=4, training=True, device=DEVICE)
        second = collator.collate(samples, epoch=4, training=True, device=DEVICE)

        assert bool(torch.equal(first.images, second.images))


class TestPadding:
    """余白へ置く位置の扱い."""

    def test_places_the_image_at_the_centre_outside_training(self, tmp_path: Path):
        dataset = _dataset(tmp_path)

        batch = _collator(augmentation=STILL).collate(
            _samples(dataset),
            epoch=0,
            training=False,
            device=DEVICE,
        )

        offsets = {
            _mask_offset(batch.valid_pixel_mask[row, 0, 0])
            for row in range(batch.images.shape[0])
        }

        assert offsets == {(1, 1)}

    def test_moves_the_image_around_during_training(self, tmp_path: Path):
        """学習時は配置をずらす.

        位置に対する不変性を訓練で経験させるための augmentation。幾何 augmentation を止めた collator
        で見るので、動いているのは配置だけ。
        """

        dataset = _dataset(tmp_path)
        samples = _samples(dataset)
        collator = _collator(augmentation=STILL)

        offsets = {
            _mask_offset(
                collator.collate(
                    samples, epoch=epoch, training=True, device=DEVICE
                ).valid_pixel_mask[0, 0, 0]
            )
            for epoch in range(12)
        }

        assert len(offsets) > 1

    def test_gives_each_sample_its_own_position(self, tmp_path: Path):
        """同じ batch の中でも sample ごとに位置が違う.

        乱数種から sample_id を落とすと、batch 全体が同じ位置へ揃う。
        """

        dataset = _dataset(tmp_path)
        samples = _samples(dataset)
        collator = _collator(augmentation=STILL)

        per_epoch = [
            {
                _mask_offset(
                    collator.collate(
                        samples,
                        epoch=epoch,
                        training=True,
                        device=DEVICE,
                    ).valid_pixel_mask[row, 0, 0]
                )
                for row in range(len(samples))
            }
            for epoch in range(12)
        ]

        assert any(len(offsets) > 1 for offsets in per_epoch)

    def test_shares_one_position_across_the_views_of_a_sample(self, tmp_path: Path):
        """1 sample の全 view は同じ位置へ置く.

        view ごとにずらすと view 間の位置合わせが壊れる。
        """

        dataset = _dataset(tmp_path)
        batch = _collator(augmentation=STILL).collate(
            _samples(dataset), epoch=3, training=True, device=DEVICE
        )

        offsets = {
            _mask_offset(batch.valid_pixel_mask[0, view, 0])
            for view in range(batch.images.shape[1])
        }

        assert len(offsets) == 1

    def test_does_not_derive_the_position_from_the_rotation(self, tmp_path: Path):
        """配置が回転角の従属変数になっていないこと.

        ``AugmentationRange.parameters_for`` は ``{global_seed}:{epoch}:augmentation:
        {sample_id}`` を sha256 に掛けた先頭 8 byte を種にする。役割ラベルを外して同じ
        材料で配置の種を作ると、両者が同じ乱数列から出て相関する。回転を有効にしたまま、
        同じ回転帯の中で複数の位置が現れることを見る。
        """

        dataset = _dataset(tmp_path)
        samples = _samples(dataset)
        collator = _collator(
            augmentation=AugmentationRange(minimum_scale=1.0, maximum_scale=1.0)
        )

        pairs: set[tuple[int, int]] = set()
        for epoch in range(120):
            batch = collator.collate(samples, epoch=epoch, training=True, device=DEVICE)
            for row, sample in enumerate(samples):
                rotation = collator.parameters_for(
                    sample.entry.sample_id, training=True, epoch=epoch
                ).rotation_degrees
                pairs.add(
                    (
                        int(rotation // ROTATION_SECTOR_DEGREES),
                        _mask_offset(batch.valid_pixel_mask[row, 0, 0])[0],
                    )
                )

        # 理論上限に張り付くので完全一致で固定する。120 epoch で 32 通り（実測。60 epoch
        # では 31 通りで、欠けるのは (帯 5, 位置 3)）。材料を共有させると epoch 数に
        # よらず 20 通りで、120 epoch でも 20 のまま（実測）
        assert len(pairs) == ROTATION_SECTORS * PLACEMENT_OFFSETS

    def test_places_the_image_from_its_labelled_seed_material(self, tmp_path: Path):
        """配置の種の材料が ``{global_seed}:{epoch}:placement:{sample_id}`` であること.

        ``_placement_seed`` を直接呼ばず、公開経路の ``valid_pixel_mask`` で観測する。

        材料から種を作る規則と ``randrange`` を引く順序（行 → 列）を test 内で
        組み直して突き合わせるので、期待値が実装そのものにならない。
        """

        dataset = _dataset(tmp_path)
        samples = _samples(dataset)

        batch = _collator(augmentation=STILL).collate(
            samples, epoch=3, training=True, device=DEVICE
        )

        assert _offsets_of(batch) == tuple(
            _expected_offset(batch, PLACEMENT_ROLE, sample.entry.sample_id, epoch=3)
            for sample in samples
        )

    def test_does_not_place_the_image_from_the_unlabelled_material(
        self, tmp_path: Path
    ):
        """上の一致検査がラベルまで見ていることの自己検査.

        役割ラベルを外した材料は augmentation の材料と一致するので、同じ位置が出てはいけない。
        """

        dataset = _dataset(tmp_path)
        samples = _samples(dataset)

        batch = _collator(augmentation=STILL).collate(
            samples, epoch=3, training=True, device=DEVICE
        )

        assert _offsets_of(batch) != tuple(
            _expected_offset(batch, None, sample.entry.sample_id, epoch=3)
            for sample in samples
        )


class TestCollateRejection:
    """呼び出し側の不変条件違反を落とすこと."""

    def test_rejects_an_empty_batch(self, tmp_path: Path):
        # 検査を外すと view 数の不一致という誤った理由で落ちるので、理由まで見る
        with pytest.raises(ValueError, match="空の batch"):
            _collator().collate((), epoch=0, training=True, device=DEVICE)

    def test_rejects_samples_whose_view_counts_differ(self, tmp_path: Path):
        """異なる view 数の sample が同じ batch へ来たら落とす.

        bucket が view 数で切っているので、ここへ来る時点で計画が壊れている。
        """

        few = _dataset(tmp_path / "few", view_count=2)
        many = _dataset(tmp_path / "many", view_count=4)

        with pytest.raises(ValueError, match="view"):
            _collator().collate(
                (
                    few.sample(few.index.entries[0].sample_id),
                    many.sample(many.index.entries[0].sample_id),
                ),
                epoch=0,
                training=False,
                device=DEVICE,
            )
