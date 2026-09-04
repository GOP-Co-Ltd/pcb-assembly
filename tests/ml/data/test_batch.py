"""可変サイズ画像の pixel budget batching と padding の公開契約."""

import pytest
import torch

from ml.data.batch import BatchShape, PaddedBatch, plan_pixel_budget_batches

BUDGET = {
    "max_batch_pixels": 8_388_608,
    "max_batch_size": 32,
    "stride": 32,
    "seed": 7,
    "epoch": 0,
}


def _shapes(count: int, *, height: int = 64, width: int = 64) -> list[BatchShape]:
    return [BatchShape(f"sample-{index:03d}", height, width) for index in range(count)]


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
        # 64x64 は stride 32 で 64x64 のまま。4 枚で 16384 px。
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

    def test_rejects_a_sample_larger_than_the_budget(self):
        with pytest.raises(ValueError, match="huge"):
            _plan([BatchShape("huge", 1024, 1024)], max_batch_pixels=1024)

    def test_returns_no_batches_for_an_empty_dataset(self):
        assert _plan([]) == ()

    def test_rejects_duplicate_sample_ids(self):
        shapes = [BatchShape("same", 64, 64), BatchShape("same", 64, 64)]

        with pytest.raises(ValueError, match="same"):
            _plan(shapes)


class TestPaddedBatchPad:
    """Batch 内の最大サイズへ stride 揃えで padding する."""

    def test_pads_to_a_stride_aligned_common_size(self):
        images = [torch.rand((6, 40, 20)), torch.rand((6, 33, 50))]
        masks = [torch.ones((1, 40, 20), dtype=torch.bool)] + [
            torch.ones((1, 33, 50), dtype=torch.bool)
        ]

        batch = PaddedBatch.pad(images, masks, placement_seeds=[1, 2])

        assert batch.images.shape == (2, 6, 64, 64)
        assert batch.valid_pixel_masks.shape == (2, 1, 64, 64)

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
