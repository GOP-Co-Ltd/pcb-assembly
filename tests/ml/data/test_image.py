"""画像前処理（サイズ制約・augmentation・SampleLayerNorm）の公開契約."""

from pathlib import Path

import pytest
import torch
from torch.nn import functional
from torchvision.io import write_png

from ml.data.image import (
    NO_AUGMENTATION,
    AugmentationParameters,
    AugmentationRange,
    ImageConstraints,
    ImageShape,
    PreprocessedSample,
    decode_rgb_image,
    sample_layer_norm,
)

CONSTRAINTS = ImageConstraints()


def _image(height: int, width: int, *, seed: int = 0) -> torch.Tensor:
    generator = torch.Generator().manual_seed(seed)
    return torch.randint(
        0, 256, (3, height, width), generator=generator, dtype=torch.uint8
    )


class TestImageConstraints:
    """V1 encoder が受け取れる画像サイズの宣言."""

    def test_defaults_match_the_documented_contract(self):
        assert CONSTRAINTS.validate() is None
        assert (CONSTRAINTS.minimum_size, CONSTRAINTS.maximum_size) == (32, 1024)
        assert CONSTRAINTS.maximum_pixels == 262_144

    @pytest.mark.parametrize(
        ("constraints", "expected"),
        [
            (ImageConstraints(minimum_size=0), "minimum_size"),
            (ImageConstraints(minimum_size=2048), "maximum_size"),
            (ImageConstraints(maximum_pixels=0), "maximum_pixels"),
            (ImageConstraints(stride=0), "stride"),
        ],
    )
    def test_rejects_inconsistent_constraints(
        self, constraints: ImageConstraints, expected: str
    ):
        error = constraints.validate()

        assert error is not None
        assert expected in error


class TestImageShapePreprocessed:
    """縮小後の高さ・幅."""

    def test_does_not_upscale_a_small_image(self):
        shape = ImageShape(64, 64).preprocessed(
            constraints=CONSTRAINTS, parameters=NO_AUGMENTATION
        )

        assert shape == ImageShape(64, 64)

    def test_is_limited_by_the_longest_side(self):
        shape = ImageShape(64, 4096).preprocessed(
            constraints=CONSTRAINTS, parameters=NO_AUGMENTATION
        )

        assert shape == ImageShape(16, 1024)

    def test_is_limited_by_the_pixel_budget(self):
        shape = ImageShape(1024, 1024).preprocessed(
            constraints=CONSTRAINTS, parameters=NO_AUGMENTATION
        )

        assert shape == ImageShape(512, 512)
        assert shape.pixels == pytest.approx(CONSTRAINTS.maximum_pixels)

    def test_keeps_an_extreme_aspect_ratio(self):
        shape = ImageShape(64, 2048).preprocessed(
            constraints=CONSTRAINTS, parameters=NO_AUGMENTATION
        )

        assert shape.width == 1024
        assert shape.height == 32

    @pytest.mark.parametrize(
        "original", [ImageShape(512, 512), ImageShape(1024, 256), ImageShape(64, 2048)]
    )
    def test_result_satisfies_every_size_constraint(self, original: ImageShape):
        shape = original.preprocessed(
            constraints=CONSTRAINTS, parameters=NO_AUGMENTATION
        )

        assert max(shape.height, shape.width) <= CONSTRAINTS.maximum_size
        assert shape.pixels <= CONSTRAINTS.maximum_pixels

    def test_augmentation_scale_is_clipped_by_the_constraints(self):
        enlarged = AugmentationParameters(rotation_degrees=0.0, scale=1.2)

        shape = ImageShape(512, 512).preprocessed(
            constraints=CONSTRAINTS, parameters=enlarged
        )

        assert shape.pixels <= CONSTRAINTS.maximum_pixels

    def test_augmentation_scale_shrinks_the_result(self):
        shrunk = AugmentationParameters(rotation_degrees=0.0, scale=0.5)

        shape = ImageShape(256, 256).preprocessed(
            constraints=CONSTRAINTS, parameters=shrunk
        )

        assert shape == ImageShape(128, 128)


class TestAugmentationRangeParametersFor:
    """Worker 数や中断再開に依存しない決定論的な変換."""

    def test_is_reproducible_for_the_same_sample_and_epoch(self):
        first = AugmentationRange().parameters_for(
            sample_id="sample-a",
            global_seed=7,
            epoch=3,
        )
        second = AugmentationRange().parameters_for(
            sample_id="sample-a",
            global_seed=7,
            epoch=3,
        )

        assert first == second

    @pytest.mark.parametrize(
        ("sample_id", "epoch", "global_seed"),
        [("sample-b", 3, 7), ("sample-a", 4, 7), ("sample-a", 3, 8)],
    )
    def test_differs_when_the_derivation_inputs_differ(
        self, sample_id: str, epoch: int, global_seed: int
    ):
        baseline = AugmentationRange().parameters_for(
            sample_id="sample-a",
            global_seed=7,
            epoch=3,
        )

        other = AugmentationRange().parameters_for(
            sample_id=sample_id,
            global_seed=global_seed,
            epoch=epoch,
        )

        assert other != baseline

    def test_stays_inside_the_declared_range(self):
        augmentation = AugmentationRange(minimum_scale=0.8, maximum_scale=1.2)

        for index in range(64):
            parameters = augmentation.parameters_for(
                sample_id=f"sample-{index}",
                global_seed=1,
                epoch=0,
            )

            assert 0.0 <= parameters.rotation_degrees < 360.0
            assert 0.8 <= parameters.scale <= 1.2

    def test_disabled_rotation_and_unit_scale_produce_no_augmentation(self):
        disabled = AugmentationRange(
            rotation_enabled=False, minimum_scale=1.0, maximum_scale=1.0
        )

        parameters = disabled.parameters_for(
            sample_id="sample-a", global_seed=7, epoch=3
        )

        assert parameters == NO_AUGMENTATION

    @pytest.mark.parametrize(
        ("augmentation", "expected"),
        [
            (AugmentationRange(minimum_scale=0.0), "minimum_scale"),
            (AugmentationRange(minimum_scale=1.5, maximum_scale=1.2), "maximum_scale"),
        ],
    )
    def test_rejects_an_inconsistent_range(
        self, augmentation: AugmentationRange, expected: str
    ):
        error = augmentation.validate()

        assert error is not None
        assert expected in error


class TestDecodeRgbImage:
    """Torchvision で RGB の CHW tensor として読む契約."""

    def test_reads_channels_in_rgb_order(self, tmp_path: Path):
        source = torch.zeros((3, 4, 5), dtype=torch.uint8)
        source[0] = 10
        source[1] = 120
        source[2] = 240
        path = tmp_path / "controlled.png"
        write_png(source, str(path), compression_level=0)

        decoded = decode_rgb_image(path)

        assert decoded.shape == (3, 4, 5)
        assert decoded.dtype == torch.uint8
        assert torch.equal(decoded, source)

    def test_expands_a_grayscale_png_to_three_channels(self, tmp_path: Path):
        path = tmp_path / "gray.png"
        write_png(torch.full((1, 4, 5), 77, dtype=torch.uint8), str(path))

        decoded = decode_rgb_image(path)

        assert decoded.shape == (3, 4, 5)
        assert torch.equal(decoded, torch.full((3, 4, 5), 77, dtype=torch.uint8))


class TestSampleLayerNorm:
    """Sample 1 個の全軸で標準化する入力正規化."""

    def test_matches_torch_layer_norm_when_every_pixel_is_valid(self):
        image = torch.rand((6, 8, 9))

        normalized, error = sample_layer_norm(image)

        assert error is None
        assert normalized is not None
        expected = functional.layer_norm(image, image.shape, None, None, 1e-5)
        assert torch.allclose(normalized, expected, atol=1e-6)

    def test_uses_only_valid_pixels_when_masked(self):
        image = torch.rand((6, 8, 9))
        valid_mask = torch.zeros((1, 8, 9), dtype=torch.bool)
        valid_mask[:, :4, :] = True

        normalized, error = sample_layer_norm(image, valid_mask=valid_mask)

        assert error is None
        assert normalized is not None
        visible = image[:, :4, :]
        mean = visible.mean()
        variance = visible.var(correction=0)
        expected = (visible - mean) / torch.sqrt(variance + 1e-5)
        assert torch.allclose(normalized[:, :4, :], expected, atol=1e-6)

    def test_ignores_non_finite_values_outside_the_mask(self):
        image = torch.rand((6, 8, 9))
        image[:, 4:, :] = float("nan")
        valid_mask = torch.zeros((1, 8, 9), dtype=torch.bool)
        valid_mask[:, :4, :] = True

        normalized, error = sample_layer_norm(image, valid_mask=valid_mask)

        assert error is None
        assert normalized is not None
        assert bool(torch.isfinite(normalized).all())
        visible = image[:, :4, :]
        expected = (visible - visible.mean()) / torch.sqrt(
            visible.var(correction=0) + 1e-5
        )
        assert torch.allclose(normalized[:, :4, :], expected, atol=1e-6)

    def test_zeroes_the_invalid_region(self):
        image = torch.rand((6, 8, 9)) + 1.0
        valid_mask = torch.zeros((1, 8, 9), dtype=torch.bool)
        valid_mask[:, :4, :] = True

        normalized, _ = sample_layer_norm(image, valid_mask=valid_mask)

        assert normalized is not None
        assert torch.all(normalized[:, 4:, :] == 0.0)

    def test_keeps_the_relation_between_stacked_images(self):
        image = torch.rand((6, 8, 9))
        brightened = image.clone()
        brightened[3:] += 0.5

        first, _ = sample_layer_norm(image)
        second, _ = sample_layer_norm(brightened)

        assert first is not None
        assert second is not None
        # 前半 channel だけを触っていないのに値が動く = 全 channel 共通の統計を使っている
        assert not torch.allclose(first[:3], second[:3], atol=1e-3)

    @pytest.mark.parametrize(
        ("image", "valid_mask"),
        [
            (torch.zeros((6, 8, 9)), None),
            (torch.rand((6, 8, 9)), torch.zeros((1, 8, 9), dtype=torch.bool)),
            (torch.full((6, 8, 9), float("nan")), None),
        ],
    )
    def test_rejects_samples_without_usable_statistics(
        self, image: torch.Tensor, valid_mask: torch.Tensor | None
    ):
        normalized, error = sample_layer_norm(image, valid_mask=valid_mask)

        assert normalized is None
        assert error is not None


class TestPreprocessedSamplePreprocess:
    """Decode 済み画像列を model 入力へそろえる一連の処理."""

    def test_concatenates_images_along_the_channel_axis(self):
        sample, error = PreprocessedSample.preprocess(
            [_image(64, 48, seed=1), _image(64, 48, seed=2)],
            constraints=CONSTRAINTS,
            parameters=NO_AUGMENTATION,
        )

        assert error is None
        assert sample is not None
        assert sample.image.shape == (6, 64, 48)
        assert sample.image.dtype == torch.float32
        assert sample.valid_mask.shape == (1, 64, 48)
        assert bool(sample.valid_mask.all())
        assert sample.scale == pytest.approx(1.0)

    def test_accepts_a_single_image(self):
        sample, error = PreprocessedSample.preprocess(
            [_image(64, 48)], constraints=CONSTRAINTS, parameters=NO_AUGMENTATION
        )

        assert error is None
        assert sample is not None
        assert sample.image.shape == (3, 64, 48)

    def test_downscales_and_reports_the_applied_scale(self):
        sample, error = PreprocessedSample.preprocess(
            [_image(128, 4096)], constraints=CONSTRAINTS, parameters=NO_AUGMENTATION
        )

        assert error is None
        assert sample is not None
        assert sample.image.shape[1:] == (32, 1024)
        assert sample.scale == pytest.approx(0.25)

    def test_rotation_marks_the_corners_invalid(self):
        rotated = AugmentationParameters(rotation_degrees=45.0, scale=1.0)

        sample, error = PreprocessedSample.preprocess(
            [_image(64, 64)], constraints=CONSTRAINTS, parameters=rotated
        )

        assert error is None
        assert sample is not None
        assert not bool(sample.valid_mask.all())
        assert not bool(sample.valid_mask[0, 0, 0])
        assert bool(sample.valid_mask[0, 32, 32])
        assert torch.all(sample.image[:, ~sample.valid_mask[0]] == 0.0)

    def test_is_normalized_over_the_valid_region(self):
        sample, _ = PreprocessedSample.preprocess(
            [_image(64, 48, seed=1), _image(64, 48, seed=2)],
            constraints=CONSTRAINTS,
            parameters=NO_AUGMENTATION,
        )

        assert sample is not None
        assert float(sample.image.mean()) == pytest.approx(0.0, abs=1e-5)
        assert float(sample.image.std(correction=0)) == pytest.approx(1.0, abs=1e-3)

    @pytest.mark.parametrize(
        ("images", "expected"),
        [
            ([], "画像"),
            ([_image(16, 64)], "minimum_size"),
            ([_image(64, 64), _image(64, 48)], "同じ"),
            ([torch.rand((3, 64, 64))], "uint8"),
            ([_image(64, 64).unsqueeze(0)], "CHW"),
        ],
    )
    def test_rejects_unusable_input(self, images: list[torch.Tensor], expected: str):
        sample, error = PreprocessedSample.preprocess(
            images, constraints=CONSTRAINTS, parameters=NO_AUGMENTATION
        )

        assert sample is None
        assert error is not None
        assert expected in error

    def test_rejects_an_image_that_downscales_below_the_minimum(self):
        sample, error = PreprocessedSample.preprocess(
            [_image(40, 4096)], constraints=CONSTRAINTS, parameters=NO_AUGMENTATION
        )

        assert sample is None
        assert error is not None
        assert "minimum_size" in error

    def test_compares_by_identity(self):
        sample, _ = PreprocessedSample.preprocess(
            [_image(64, 48, seed=1)],
            constraints=CONSTRAINTS,
            parameters=NO_AUGMENTATION,
        )
        different_image, _ = PreprocessedSample.preprocess(
            [_image(64, 48, seed=2)],
            constraints=CONSTRAINTS,
            parameters=NO_AUGMENTATION,
        )

        assert sample is not None
        assert different_image is not None
        assert sample.scale == different_image.scale
        assert sample == sample
        assert sample != different_image
        assert len({sample, different_image}) == 2
