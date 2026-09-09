"""可変サイズ画像の pixel budget batching と padding の公開契約."""

import random

import pytest
import torch

from ml.data.batch import (
    BatchShape,
    MultiViewPaddedBatch,
    PaddedBatch,
    ViewDropout,
    plan_pixel_budget_batches,
)

# 点塗布 crop の実寸。53 px は stride 8 で 56 px へ切り上がる
CROP_SIZE = 53
PADDED_CROP_SIZE = 56

BUDGET = {
    "max_batch_pixels": 8_388_608,
    "max_batch_size": 32,
    "stride": 8,
    "seed": 7,
    "epoch": 0,
}


def _shapes(
    count: int, *, height: int = 64, width: int = 64, view_count: int = 1
) -> list[BatchShape]:
    return [
        BatchShape(f"sample-{index:03d}", height, width, view_count)
        for index in range(count)
    ]


def _plan(shapes: list[BatchShape], **overrides) -> tuple[tuple[str, ...], ...]:
    return plan_pixel_budget_batches(shapes, **(BUDGET | overrides))


class TestPlanPixelBudgetBatches:
    """Aspect と面積で bucket 化し、padding 後の画素数で詰める."""

    def test_covers_every_sample_exactly_once(self):
        shapes = _shapes(37)

        plan = _plan(shapes)

        planned = [sample_id for batch in plan for sample_id in batch]
        assert sorted(planned) == sorted(shape.sample_id for shape in shapes)

    def test_is_reproducible_for_the_same_seed_and_epoch(self):
        shapes = _shapes(20)

        assert _plan(shapes) == _plan(shapes)

    def test_differs_between_epochs(self):
        shapes = _shapes(40)

        assert _plan(shapes, epoch=0) != _plan(shapes, epoch=1)

    def test_is_independent_of_the_input_order(self):
        shapes = _shapes(20)
        reversed_shapes = list(reversed(shapes))

        assert _plan(shapes) == _plan(reversed_shapes)

    def test_respects_the_batch_size_limit(self):
        plan = _plan(_shapes(20), max_batch_size=4)

        assert plan != ()
        assert max(len(batch) for batch in plan) <= 4

    def test_respects_the_pixel_budget(self):
        # 64x64 は stride 8 でも 64x64 のまま。4 枚で 16384 px。
        plan = _plan(_shapes(20), max_batch_pixels=16_384)

        assert max(len(batch) for batch in plan) <= 4

    def test_keeps_the_last_small_batch(self):
        plan = _plan(_shapes(9), max_batch_size=4)

        assert sorted(len(batch) for batch in plan) == [1, 4, 4]

    def test_separates_different_aspect_ratios(self):
        shapes = [
            BatchShape("wide-1", 64, 1024),
            BatchShape("wide-2", 64, 1024),
            BatchShape("tall-1", 1024, 64),
            BatchShape("tall-2", 1024, 64),
        ]

        plan = _plan(shapes)

        for batch in plan:
            prefixes = {sample_id.split("-")[0] for sample_id in batch}
            assert len(prefixes) == 1

    def test_separates_mixed_crop_sizes(self):
        """``crop_size_mm`` の違う session を混ぜた composite で bucket が割れる.

        1 session の中では crop が均一なので、面積 bucket は 1 個へ潰れる。

        bucket 鍵の面積項が意味を持つのは複数 session を混ぜたときだけ。

        そこを観測点にし、batch 数の下限ではなく厳密な個数を見る。
        """

        shapes = [
            BatchShape(f"crop{size}-{index}", size, size)
            for size in (27, 53, 159)
            for index in range(4)
        ]

        plan = _plan(shapes)

        assert len(plan) == 3
        for batch in plan:
            assert len({sample_id.split("-")[0] for sample_id in batch}) == 1

    def test_separates_different_view_counts(self):
        """View 数の違う sample は同じ batch へ入れない.

        ``[B, V, C, H, W]`` は batch 内で V が揃っている必要がある。view 軸の
        padding と view 妥当性 mask を持ち込まないための制約。
        """

        shapes = [
            BatchShape(f"views{view_count}-{index}", CROP_SIZE, CROP_SIZE, view_count)
            for view_count in (1, 5)
            for index in range(4)
        ]

        plan = _plan(shapes)

        assert len(plan) == 2
        for batch in plan:
            assert len({sample_id.split("-")[0] for sample_id in batch}) == 1

    @pytest.mark.parametrize(("view_count", "expected"), [(1, 2), (5, 10)])
    def test_the_pixel_budget_counts_every_view(self, view_count: int, expected: int):
        """予算は view 数を掛けて評価する.

        53x53 は stride 8 で 56x56（3136 px）になる。

        予算を 5 枚ぶんに置くと、単視点なら 5 sample ずつ 2 batch。

        5 視点なら 1 sample ずつ 10 batch へ割れる。

        コストから view 数を落とす変異は、5 視点側の batch 数で必ず落ちる。
        """

        plan = _plan(
            _shapes(10, height=CROP_SIZE, width=CROP_SIZE, view_count=view_count),
            max_batch_pixels=5 * PADDED_CROP_SIZE * PADDED_CROP_SIZE,
        )

        assert len(plan) == expected

    def test_rejects_a_sample_larger_than_the_budget(self):
        with pytest.raises(ValueError, match="huge"):
            _plan([BatchShape("huge", 1024, 1024)], max_batch_pixels=1024)

    def test_returns_no_batches_for_an_empty_dataset(self):
        assert _plan([]) == ()

    @pytest.mark.parametrize(
        ("height", "width", "view_count"),
        [(0, 64, 1), (64, 0, 1), (64, 64, 0)],
    )
    def test_rejects_a_shape_that_cannot_consume_the_pixel_budget(
        self, height: int, width: int, view_count: int
    ):
        """0 以下の辺・view 数は予算計算を無効化するので入口で弾く.

        画素数が 0 になると budget を超えることが無く、1 batch へ無制限に詰まる。
        """

        with pytest.raises(ValueError, match="正の整数"):
            _plan([BatchShape("degenerate", height, width, view_count)])

    def test_rejects_duplicate_sample_ids(self):
        shapes = [BatchShape("same", 64, 64), BatchShape("same", 64, 64)]

        with pytest.raises(ValueError, match="same"):
            _plan(shapes)

    def test_rejects_a_multi_view_sample_larger_than_the_budget(self):
        """1 sample の予算超過は view 数を掛けた画素数で判定する.

        view 数を落とすと 5 分の 1 の見積りになり、超過を見逃す。
        """

        with pytest.raises(ValueError, match="wide-view"):
            _plan([BatchShape("wide-view", 64, 64, 5)], max_batch_pixels=64 * 64 * 4)

    def test_the_single_sample_check_counts_the_stride_aligned_size(self):
        """1 sample の予算超過も padding 後の 56x56 で判定する.

        源の 53x53 で数えると、padding するとあふれる sample を通してしまう。
        """

        with pytest.raises(ValueError, match="tight"):
            _plan(
                [BatchShape("tight", CROP_SIZE, CROP_SIZE)],
                max_batch_pixels=CROP_SIZE**2,
            )

    def test_the_pixel_budget_counts_the_stride_aligned_size(self):
        """予算は padding 後の 56x56 で数える.

        源の 53x53 で数えると 1 batch へ実際より多くの sample が入る。
        """

        plan = _plan(
            _shapes(4, height=CROP_SIZE, width=CROP_SIZE),
            max_batch_pixels=2 * PADDED_CROP_SIZE**2 - 1,
        )

        assert max(len(batch) for batch in plan) == 1

    @pytest.mark.parametrize("field", ["max_batch_pixels", "max_batch_size"])
    def test_rejects_a_non_positive_budget(self, field: str):
        """0 以下の予算は「正の整数」の理由で弾く.

        入口を消すと ``max_batch_pixels=0`` は「1 sample が超えます」へ、
        ``max_batch_size=0`` は無言の 1 件ずつ分割へ化ける。
        """

        with pytest.raises(ValueError, match="正の整数"):
            _plan(_shapes(2), **{field: 0})

    def test_rejects_a_non_positive_stride(self):
        """0 の stride は ``ZeroDivisionError`` になる前に弾く."""

        with pytest.raises(ValueError, match="stride"):
            _plan(_shapes(2), stride=0)

    def test_is_independent_of_the_input_order_across_buckets(self):
        """Bucket が複数あっても入力順で計画が変わらない.

        bucket 鍵の巡回順を dict の挿入順にすると、入力順で並びが変わる。
        """

        shapes = [
            *_shapes(3, height=32, width=32),
            *[BatchShape(f"tall-{index}", 1024, 64) for index in range(3)],
            *[BatchShape(f"wide-{index}", 64, 1024) for index in range(3)],
            *[BatchShape(f"multi-{index}", 32, 32, 5) for index in range(3)],
            *[BatchShape(f"big-{index}", 256, 256) for index in range(3)],
        ]

        assert _plan(shapes) == _plan(list(reversed(shapes)))


class TestPaddedBatchPad:
    """Batch 内の最大サイズへ stride 揃えで padding する."""

    def test_pads_to_a_stride_aligned_common_size(self):
        images = [torch.rand((6, 40, 20)), torch.rand((6, 33, 50))]
        masks = [torch.ones((1, 40, 20), dtype=torch.bool)] + [
            torch.ones((1, 33, 50), dtype=torch.bool)
        ]

        batch = PaddedBatch.pad(images, masks, placement_seeds=[1, 2])

        # 既定 stride 8。高さ 40 はそのまま、幅 50 は 56 へ切り上がる
        assert batch.images.shape == (2, 6, 40, 56)
        assert batch.valid_pixel_masks.shape == (2, 1, 40, 56)

    def test_the_default_stride_matches_the_image_constraints(self):
        """既定 stride は 8。53 px の crop は 56 px へ切り上がる.

        ``ImageConstraints.stride`` と ``PaddedBatch.pad(stride=)`` は別々の値を
        取りうる二重管理なので、既定値がそろっていることを固定する。
        stride 32 へ戻すと 64 px になり、無駄 padding が 10.4% から 31.4% へ増える。
        """

        image = torch.rand((6, CROP_SIZE, CROP_SIZE))
        mask = torch.ones((1, CROP_SIZE, CROP_SIZE), dtype=torch.bool)

        batch = PaddedBatch.pad([image], [mask], placement_seeds=[0])

        assert tuple(batch.images.shape) == (
            1,
            6,
            PADDED_CROP_SIZE,
            PADDED_CROP_SIZE,
        )
        # 均一サイズでも stride 切り上げが padding を作るので mask は all-true でない
        assert not bool(batch.valid_pixel_masks.all())
        assert int(batch.valid_pixel_masks.sum()) == CROP_SIZE * CROP_SIZE

    def test_centres_each_image_during_evaluation(self):
        image = torch.full((3, 32, 32), 0.5)
        mask = torch.ones((1, 32, 32), dtype=torch.bool)

        batch = PaddedBatch.pad(
            [image, torch.rand((3, 64, 64))],
            [mask, torch.ones((1, 64, 64), dtype=torch.bool)],
            placement_seeds=[1, 2],
        )

        assert bool(batch.valid_pixel_masks[0, 0, 16, 16])
        assert not bool(batch.valid_pixel_masks[0, 0, 0, 0])

    def test_training_placement_is_seed_deterministic(self):
        images = [torch.rand((3, 32, 32)), torch.rand((3, 64, 64))]
        masks = [
            torch.ones((1, 32, 32), dtype=torch.bool),
            torch.ones((1, 64, 64), dtype=torch.bool),
        ]

        first = PaddedBatch.pad(images, masks, placement_seeds=[5, 6], training=True)
        second = PaddedBatch.pad(images, masks, placement_seeds=[5, 6], training=True)

        assert torch.equal(first.valid_pixel_masks, second.valid_pixel_masks)

    def test_training_placement_is_not_the_evaluation_centre(self):
        """学習時は seed から決めた位置へずらす.

        ``training`` を見ずに常に中央へ置く変異は、決定論だけを見るテストでは
        素通りする。
        """

        images = [torch.rand((3, 32, 32)), torch.rand((3, 64, 64))]
        masks = [
            torch.ones((1, 32, 32), dtype=torch.bool),
            torch.ones((1, 64, 64), dtype=torch.bool),
        ]

        centred = PaddedBatch.pad(images, masks, placement_seeds=[5, 6])
        shifted = PaddedBatch.pad(images, masks, placement_seeds=[5, 6], training=True)

        assert not torch.equal(centred.valid_pixel_masks, shifted.valid_pixel_masks)

    def test_rejects_a_non_positive_stride(self):
        """0 の stride は ``ZeroDivisionError`` になる前に弾く."""

        with pytest.raises(ValueError, match="stride"):
            PaddedBatch.pad(
                [torch.rand((3, 32, 32))],
                [torch.ones((1, 32, 32), dtype=torch.bool)],
                placement_seeds=[0],
                stride=0,
            )

    @pytest.mark.parametrize("training", [False, True])
    def test_places_the_image_where_the_mask_says_it_is(self, training: bool):
        """画像は mask が示す位置そのものへ置く.

        mask の画素数や 1 点だけを見るテストでは、画像を別の位置へ書く欠陥を
        観測できない。

        実在すると mask が画像の無い位置を有効と記録し、``replace_invalid_pixels``
        が本物の画素を learnable padding pixel へ置き換える。
        """

        images = [torch.full((3, 32, 32), 0.5), torch.full((3, 56, 56), 0.5)]
        masks = [
            torch.ones((1, 32, 32), dtype=torch.bool),
            torch.ones((1, 56, 56), dtype=torch.bool),
        ]

        batch = PaddedBatch.pad(
            images, masks, placement_seeds=[5, 6], training=training
        )

        # 全 true だと配置の食い違いが観測できない。padding が在ることを先に見る
        assert not bool(batch.valid_pixel_masks.all())
        occupied = batch.images.ne(0.0).all(dim=1, keepdim=True)
        assert torch.equal(occupied, batch.valid_pixel_masks)

    def test_valid_mask_marks_exactly_the_placed_pixels(self):
        image = torch.rand((3, 32, 48))
        mask = torch.ones((1, 32, 48), dtype=torch.bool)

        batch = PaddedBatch.pad([image], [mask], placement_seeds=[0])

        assert int(batch.valid_pixel_masks.sum()) == 32 * 48

    def test_preserves_a_partially_invalid_sample_mask(self):
        mask = torch.ones((1, 32, 32), dtype=torch.bool)
        mask[0, :8, :] = False

        batch = PaddedBatch.pad([torch.rand((3, 32, 32))], [mask], placement_seeds=[0])

        assert int(batch.valid_pixel_masks.sum()) == 24 * 32

    @pytest.mark.parametrize(
        ("images", "masks", "expected"),
        [
            ([], [], "空"),
            (
                [torch.rand((3, 32, 32)), torch.rand((6, 32, 32))],
                [torch.ones((1, 32, 32), dtype=torch.bool)] * 2,
                "channel",
            ),
            (
                [torch.rand((3, 32, 32))],
                [torch.ones((1, 16, 16), dtype=torch.bool)],
                "mask",
            ),
            (
                [torch.rand((3, 32, 32))],
                [torch.ones((1, 32, 32), dtype=torch.float32)],
                "mask",
            ),
            (
                [torch.rand((3, 32, 32)), torch.rand((3, 32, 32), dtype=torch.float64)],
                [torch.ones((1, 32, 32), dtype=torch.bool)] * 2,
                "dtype",
            ),
            # device 違いは meta device で観測する。GPU の有無に依存させない
            (
                [torch.rand((3, 32, 32))],
                [torch.ones((1, 32, 32), dtype=torch.bool, device="meta")],
                "device が一致しません",
            ),
            (
                [torch.rand((3, 32, 32)), torch.rand((3, 32, 32), device="meta")],
                [torch.ones((1, 32, 32), dtype=torch.bool)] * 2,
                "device と dtype は統一",
            ),
        ],
    )
    def test_rejects_inconsistent_input(
        self,
        images: list[torch.Tensor],
        masks: list[torch.Tensor],
        expected: str,
    ):
        with pytest.raises(ValueError, match=expected):
            PaddedBatch.pad(images, masks, placement_seeds=list(range(len(images))))

    def test_rejects_a_placement_seed_count_mismatch(self):
        with pytest.raises(ValueError, match="件数"):
            PaddedBatch.pad(
                [torch.rand((3, 32, 32))],
                [torch.ones((1, 32, 32), dtype=torch.bool)],
                placement_seeds=[1, 2],
            )

    def test_compares_by_identity(self):
        images = [torch.rand((3, 32, 32))]
        masks = [torch.ones((1, 32, 32), dtype=torch.bool)]

        batch = PaddedBatch.pad(images, masks, placement_seeds=[0])
        same_input = PaddedBatch.pad(images, masks, placement_seeds=[0])

        assert batch == batch
        assert batch != same_input
        assert len({batch, same_input}) == 2


def _views(view_count: int, height: int, width: int) -> torch.Tensor:
    return torch.rand((view_count, 6, height, width))


class TestMultiViewPaddedBatchPad:
    """View 軸を持つ画像を batch 内の最大サイズへそろえる."""

    def test_produces_a_batch_view_channel_height_width_tensor(self):
        images = [_views(5, 40, 20), _views(5, 33, 50)]
        masks = [
            torch.ones((1, 40, 20), dtype=torch.bool),
            torch.ones((1, 33, 50), dtype=torch.bool),
        ]

        batch = MultiViewPaddedBatch.pad(images, masks, placement_seeds=[1, 2])

        assert tuple(batch.images.shape) == (2, 5, 6, 40, 56)
        assert tuple(batch.valid_pixel_masks.shape) == (2, 5, 1, 40, 56)

    def test_pads_a_uniform_crop_to_the_stride_and_leaves_padding_in_the_mask(self):
        """均一 53 px + stride 8 でも mask は all-true にならない.

        「多視点・均一 crop なので mask は常に全 true」という思い込みで mask
        経路を省く変異を、有効画素の厳密な枚数で捕まえる。
        """

        images = [_views(3, CROP_SIZE, CROP_SIZE) for _ in range(2)]
        masks = [torch.ones((1, CROP_SIZE, CROP_SIZE), dtype=torch.bool)] * 2

        batch = MultiViewPaddedBatch.pad(images, masks, placement_seeds=[1, 2])

        assert tuple(batch.images.shape) == (
            2,
            3,
            6,
            PADDED_CROP_SIZE,
            PADDED_CROP_SIZE,
        )
        assert not bool(batch.valid_pixel_masks.all())
        assert int(batch.valid_pixel_masks.sum()) == 2 * 3 * CROP_SIZE * CROP_SIZE

    def test_broadcasts_the_shared_mask_to_every_view(self):
        """View 共通の ``[1, H, W]`` mask を全 view へ同じ形で展開する."""

        mask = torch.ones((1, 32, 32), dtype=torch.bool)
        mask[0, :8, :] = False

        batch = MultiViewPaddedBatch.pad(
            [_views(4, 32, 32)], [mask], placement_seeds=[0]
        )

        for view in range(1, 4):
            assert torch.equal(
                batch.valid_pixel_masks[0, 0], batch.valid_pixel_masks[0, view]
            )
        assert int(batch.valid_pixel_masks.sum()) == 4 * 24 * 32

    @pytest.mark.parametrize("training", [False, True])
    def test_places_the_image_where_the_mask_says_it_is(self, training: bool):
        """画像は mask が示す位置そのものへ置く.

        mask だけを正しく置いて画像を別の位置へ書く欠陥は、mask だけを見る
        テストでは観測できない。

        実在すると mask が画像の無い位置を有効と記録し、``replace_invalid_pixels``
        が本物の画素を learnable padding pixel へ置き換える。
        """

        images = [torch.full((3, 6, 32, 32), 0.5), torch.full((3, 6, 56, 56), 0.5)]
        masks = [
            torch.ones((1, 32, 32), dtype=torch.bool),
            torch.ones((1, 56, 56), dtype=torch.bool),
        ]

        batch = MultiViewPaddedBatch.pad(
            images, masks, placement_seeds=[5, 6], training=training
        )

        # 全 true だと配置の食い違いが観測できない。padding が在ることを先に見る
        assert not bool(batch.valid_pixel_masks.all())
        occupied = batch.images.ne(0.0).all(dim=2, keepdim=True)
        assert torch.equal(occupied, batch.valid_pixel_masks)

    def test_centres_each_image_during_evaluation(self):
        """評価時は batch canvas の中央へ置く.

        56 px canvas の 32 px 画像は上下左右 12 px ずつ空く。

        配置を左上へ固定する変異は 11 行 11 列目が有効になって落ちる。
        """

        images = [_views(3, 32, 32), _views(3, 56, 56)]
        masks = [
            torch.ones((1, 32, 32), dtype=torch.bool),
            torch.ones((1, 56, 56), dtype=torch.bool),
        ]

        batch = MultiViewPaddedBatch.pad(images, masks, placement_seeds=[1, 2])

        assert bool(batch.valid_pixel_masks[0, 0, 0, 12, 12])
        assert not bool(batch.valid_pixel_masks[0, 0, 0, 11, 11])

    def test_training_placement_is_not_the_evaluation_centre(self):
        """学習時は seed から決めた位置へずらす.

        ``training`` を見ずに常に中央へ置く変異は、決定論だけを見るテストでは
        素通りする。
        """

        images = [_views(3, 32, 32), _views(3, 56, 56)]
        masks = [
            torch.ones((1, 32, 32), dtype=torch.bool),
            torch.ones((1, 56, 56), dtype=torch.bool),
        ]

        centred = MultiViewPaddedBatch.pad(images, masks, placement_seeds=[5, 6])
        shifted = MultiViewPaddedBatch.pad(
            images, masks, placement_seeds=[5, 6], training=True
        )

        assert not torch.equal(centred.valid_pixel_masks, shifted.valid_pixel_masks)

    def test_places_every_view_of_a_sample_at_the_same_position(self):
        """1 sample の全 view は同じ位置へ置く.

        View ごとに配置をずらすと view 間の位置合わせが壊れる。学習経路
        （``training=True``）でも 1 sample 1 位置であることを見る。
        """

        images = [_views(3, 32, 32), _views(3, 56, 56)]
        masks = [
            torch.ones((1, 32, 32), dtype=torch.bool),
            torch.ones((1, 56, 56), dtype=torch.bool),
        ]

        batch = MultiViewPaddedBatch.pad(
            images, masks, placement_seeds=[5, 6], training=True
        )

        for view in range(1, 3):
            assert torch.equal(
                batch.valid_pixel_masks[0, 0], batch.valid_pixel_masks[0, view]
            )

    def test_training_placement_is_seed_deterministic(self):
        images = [_views(2, 32, 32), _views(2, 56, 56)]
        masks = [
            torch.ones((1, 32, 32), dtype=torch.bool),
            torch.ones((1, 56, 56), dtype=torch.bool),
        ]

        first = MultiViewPaddedBatch.pad(
            images, masks, placement_seeds=[5, 6], training=True
        )
        second = MultiViewPaddedBatch.pad(
            images, masks, placement_seeds=[5, 6], training=True
        )

        assert torch.equal(first.valid_pixel_masks, second.valid_pixel_masks)

    def test_rejects_a_batch_whose_view_counts_disagree(self):
        """View 数が不揃いな batch は組めない.

        view 軸の padding と view 妥当性 mask を持ち込まない設計なので、
        呼び出し側の不変条件違反として例外にする（``PaddedBatch.pad`` と同じ作法）。
        """

        images = [_views(5, 32, 32), _views(3, 32, 32)]
        masks = [torch.ones((1, 32, 32), dtype=torch.bool)] * 2

        with pytest.raises(ValueError, match="view 数"):
            MultiViewPaddedBatch.pad(images, masks, placement_seeds=[1, 2])

    @pytest.mark.parametrize(
        "images",
        [
            [torch.rand((6, 32, 32))],
            [torch.rand(6)],
            [_views(2, 32, 32), torch.rand((6, 32, 32))],
        ],
        ids=["first-chw", "first-1d", "second-chw"],
    )
    def test_rejects_images_that_lack_a_view_axis(self, images: list[torch.Tensor]):
        """先頭も 2 件目も、view 軸が無ければ同じ理由で弾く.

        先頭だけの検査と loop の検査は互いを隠すので、``images[0]`` の軸数が
        2 未満のケースと、2 件目が壊れているケースの両方を置く。
        """

        with pytest.raises(ValueError, match=r"\[V, C, H, W\]"):
            MultiViewPaddedBatch.pad(
                images,
                [torch.ones((1, 32, 32), dtype=torch.bool)] * len(images),
                placement_seeds=list(range(len(images))),
            )

    @pytest.mark.parametrize(
        ("images", "masks", "expected"),
        [
            ([], [], "空"),
            (
                [_views(2, 32, 32), torch.rand((2, 3, 32, 32))],
                [torch.ones((1, 32, 32), dtype=torch.bool)] * 2,
                "channel",
            ),
            (
                [_views(2, 32, 32)],
                [torch.ones((1, 16, 16), dtype=torch.bool)],
                "mask",
            ),
            (
                [_views(2, 32, 32)],
                [torch.ones((1, 32, 32), dtype=torch.float32)],
                "mask",
            ),
            (
                [_views(2, 32, 32), torch.rand((2, 6, 32, 32), dtype=torch.float64)],
                [torch.ones((1, 32, 32), dtype=torch.bool)] * 2,
                "dtype",
            ),
        ],
    )
    def test_rejects_inconsistent_input(
        self,
        images: list[torch.Tensor],
        masks: list[torch.Tensor],
        expected: str,
    ):
        with pytest.raises(ValueError, match=expected):
            MultiViewPaddedBatch.pad(
                images, masks, placement_seeds=list(range(len(images)))
            )

    def test_rejects_a_placement_seed_count_mismatch(self):
        with pytest.raises(ValueError, match="件数"):
            MultiViewPaddedBatch.pad(
                [_views(2, 32, 32)],
                [torch.ones((1, 32, 32), dtype=torch.bool)],
                placement_seeds=[1, 2],
            )

    def test_compares_by_identity(self):
        images = [_views(2, 32, 32)]
        masks = [torch.ones((1, 32, 32), dtype=torch.bool)]

        batch = MultiViewPaddedBatch.pad(images, masks, placement_seeds=[0])
        same_input = MultiViewPaddedBatch.pad(images, masks, placement_seeds=[0])

        assert batch == batch
        assert batch != same_input
        assert len({batch, same_input}) == 2


SAMPLE_IDS = ("sample-a", "sample-b", "sample-c")


class TestViewDropout:
    """学習時に batch 単位で view を間引く設定."""

    def test_rejects_a_non_positive_minimum(self):
        assert ViewDropout(minimum_view_count=0).validate() == (
            "minimum_view_count は正の整数が必要です: 0"
        )

    def test_accepts_a_positive_minimum(self):
        assert ViewDropout().validate() is None

    def test_keeps_every_view_by_default(self):
        """既定の下限は 1 で、1 view しか無い session でも通る."""

        dropout = ViewDropout()

        assert dropout.minimum_view_count == 1
        indices, error = dropout.view_indices_for(
            SAMPLE_IDS, available_view_count=1, global_seed=7, epoch=0
        )

        assert error is None
        assert indices == ((0,),) * len(SAMPLE_IDS)

    def test_depends_on_the_sample_ids(self):
        """同じ seed と epoch でも、batch の顔ぶれが違えば選び方が変わる.

        seed 材料から ``sample_ids`` を落とすと、全 batch が同じ view 番号を
        使い続ける。
        """

        dropout = ViewDropout(minimum_view_count=1)
        arguments = {"available_view_count": 5, "global_seed": 7, "epoch": 3}

        first, _ = dropout.view_indices_for(SAMPLE_IDS, **arguments)
        second, _ = dropout.view_indices_for(
            [f"other-{sample_id}" for sample_id in SAMPLE_IDS], **arguments
        )

        assert first != second

    def test_keeps_the_view_count_uniform_inside_the_batch(self):
        """Batch 内の全 sample が同じ枚数の view を残す.

        ``[B, V, C, H, W]`` を組むための前提。sample ごとに枚数を選ぶ変異は
        ここで落ちる。
        """

        indices, error = ViewDropout(minimum_view_count=1).view_indices_for(
            SAMPLE_IDS, available_view_count=5, global_seed=7, epoch=0
        )

        assert error is None
        assert indices is not None
        assert len(indices) == len(SAMPLE_IDS)
        assert len({len(row) for row in indices}) == 1

    def test_every_row_is_sorted_and_free_of_duplicates(self):
        indices, error = ViewDropout(minimum_view_count=2).view_indices_for(
            SAMPLE_IDS, available_view_count=5, global_seed=7, epoch=3
        )

        assert error is None
        assert indices is not None
        for row in indices:
            assert list(row) == sorted(row)
            assert len(set(row)) == len(row)
            assert set(row) <= set(range(5))

    def test_the_kept_count_varies_between_batches(self):
        """Batch ごとに残す view 数を選び直す.

        ``minimum_view_count`` へ固定する変異は 1 種類しか出せない。

        ``2 <= len <= 5`` の範囲 assert はその変異を通すので置かない。

        16 epoch で許される 4 通りが全て現れることを固定する。
        """

        dropout = ViewDropout(minimum_view_count=2)
        counts: set[int] = set()

        for epoch in range(16):
            indices, error = dropout.view_indices_for(
                SAMPLE_IDS, available_view_count=5, global_seed=7, epoch=epoch
            )

            assert error is None
            assert indices is not None
            counts.add(len(indices[0]))

        assert counts == {2, 3, 4, 5}

    def test_each_sample_gets_its_own_subset_of_views(self):
        """同じ batch でも sample ごとに残す view の組み合わせが違う.

        ``generator.sample`` を loop の外へ出す変異は、batch 内の全 sample へ
        同じ部分集合を配る。

        枚数だけは揃える必要があるので、長さ 1 種類と部分集合 3 種類を同時に見る。
        """

        indices, error = ViewDropout(minimum_view_count=1).view_indices_for(
            SAMPLE_IDS, available_view_count=5, global_seed=7, epoch=0
        )

        assert error is None
        assert indices is not None
        # 全 view を残す回では部分集合が一致してしまい、主張を観測できない
        assert len(indices[0]) < 5
        assert len({len(row) for row in indices}) == 1
        assert len(set(indices)) == len(SAMPLE_IDS)

    def test_is_reproducible_for_the_same_arguments(self):
        dropout = ViewDropout(minimum_view_count=1)
        arguments = {"available_view_count": 5, "global_seed": 7, "epoch": 3}

        first, _ = dropout.view_indices_for(SAMPLE_IDS, **arguments)
        second, _ = dropout.view_indices_for(SAMPLE_IDS, **arguments)

        assert first == second

    def test_does_not_depend_on_the_global_random_state(self):
        """大域乱数状態を挟んでも同じ結果になる.

        ``random.shuffle`` や ``random.randint`` を module 直下の乱数で書く変異は、
        間に乱数を消費するだけで結果が変わるのでここで落ちる。
        """

        dropout = ViewDropout(minimum_view_count=1)
        arguments = {"available_view_count": 5, "global_seed": 7, "epoch": 3}

        random.seed(0)
        first, _ = dropout.view_indices_for(SAMPLE_IDS, **arguments)
        random.seed(999)
        for _ in range(10):
            random.random()
        second, _ = dropout.view_indices_for(SAMPLE_IDS, **arguments)

        assert first == second

    def test_differs_between_epochs(self):
        dropout = ViewDropout(minimum_view_count=1)

        plans = {
            dropout.view_indices_for(
                SAMPLE_IDS, available_view_count=5, global_seed=7, epoch=epoch
            )[0]
            for epoch in range(8)
        }

        assert len(plans) > 1

    def test_keeps_every_view_when_the_minimum_equals_what_is_available(self):
        """下限と利用可能枚数が一致すると、間引きは実質無効になる."""

        indices, error = ViewDropout(minimum_view_count=5).view_indices_for(
            SAMPLE_IDS, available_view_count=5, global_seed=7, epoch=0
        )

        assert error is None
        assert indices == ((0, 1, 2, 3, 4),) * len(SAMPLE_IDS)

    def test_reports_a_session_with_fewer_views_than_the_minimum(self):
        """1 view で収集した session に下限 3 を当てると理由文字列を返す.

        実データで起こりうる不整合なので例外にしない。理由文には両方の値を出す。
        """

        indices, error = ViewDropout(minimum_view_count=3).view_indices_for(
            SAMPLE_IDS, available_view_count=1, global_seed=7, epoch=0
        )

        assert indices is None
        assert error is not None
        assert "available_view_count=1" in error
        assert "minimum_view_count=3" in error

    def test_reports_an_invalid_configuration_instead_of_selecting_views(self):
        indices, error = ViewDropout(minimum_view_count=0).view_indices_for(
            SAMPLE_IDS, available_view_count=5, global_seed=7, epoch=0
        )

        assert indices is None
        assert error == "minimum_view_count は正の整数が必要です: 0"
