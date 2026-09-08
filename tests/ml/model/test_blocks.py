"""画像 encoder を構成する畳み込み部品の公開契約."""

import attrs
import pytest
import torch

from ml.model.blocks import (
    GroupNormResidualBlock,
    ImageEncoder,
    ImageEncoderConfig,
    build_group_norm_convolution,
    build_group_norm_residual_stage,
    replace_invalid_pixels,
)

CONFIG = ImageEncoderConfig(
    input_channels=3,
    stem_channels=(8, 16),
    stem_strides=(2, 2),
    stage_channels=(16, 32),
    stage_strides=(1, 2),
    blocks_per_stage=(1, 1),
    group_norm_groups=4,
)


def _images(count: int, *, height: int = 32, width: int = 32) -> torch.Tensor:
    generator = torch.Generator().manual_seed(11)
    return torch.randn(
        (count, CONFIG.input_channels, height, width), generator=generator
    )


def _mask(images: torch.Tensor, *, valid: bool = True) -> torch.Tensor:
    shape = (int(images.shape[0]), 1, int(images.shape[2]), int(images.shape[3]))
    return torch.full(shape, valid, dtype=torch.bool)


def _with_stem_strides(stem_strides: tuple[int, ...]) -> ImageEncoderConfig:
    return attrs.evolve(CONFIG, stem_strides=stem_strides)


def _encoder() -> ImageEncoder:
    torch.manual_seed(3)
    return ImageEncoder(CONFIG).eval()


class TestBuildGroupNormConvolution:
    """Conv2d -> GroupNorm -> ReLU を 1 単位として組む."""

    def test_keeps_the_resolution_with_stride_one(self):
        block = build_group_norm_convolution(3, 8, stride=1, groups=4)

        outputs = block(torch.randn(2, 3, 16, 16))

        assert tuple(outputs.shape) == (2, 8, 16, 16)

    def test_halves_the_resolution_with_stride_two(self):
        block = build_group_norm_convolution(3, 8, stride=2, groups=4)

        outputs = block(torch.randn(2, 3, 16, 16))

        assert tuple(outputs.shape) == (2, 8, 8, 8)

    def test_output_is_non_negative_after_the_activation(self):
        block = build_group_norm_convolution(3, 8, stride=1, groups=4)

        outputs = block(torch.randn(2, 3, 16, 16))

        assert bool((outputs >= 0).all())

    def test_rejects_channels_that_are_not_divisible_by_groups(self):
        with pytest.raises(ValueError, match="groups"):
            build_group_norm_convolution(3, 10, stride=1, groups=4)

    @pytest.mark.parametrize("stride", [0, -1])
    def test_rejects_a_non_positive_stride(self, stride: int):
        with pytest.raises(ValueError, match="stride"):
            build_group_norm_convolution(3, 8, stride=stride, groups=4)

    @pytest.mark.parametrize(("in_channels", "out_channels"), [(0, 8), (3, 0)])
    def test_rejects_non_positive_channels(self, in_channels: int, out_channels: int):
        with pytest.raises(ValueError, match="channel"):
            build_group_norm_convolution(in_channels, out_channels, stride=1, groups=4)


class TestGroupNormResidualBlock:
    """3x3 畳み込み 2 段の residual block."""

    def test_changes_channels_and_resolution_with_stride_two(self):
        block = GroupNormResidualBlock(16, 32, stride=2, groups=4)

        outputs = block(torch.randn(2, 16, 16, 16))

        assert tuple(outputs.shape) == (2, 32, 8, 8)

    def test_keeps_the_shape_when_channels_and_stride_are_unchanged(self):
        block = GroupNormResidualBlock(16, 16, stride=1, groups=4)

        outputs = block(torch.randn(2, 16, 16, 16))

        assert tuple(outputs.shape) == (2, 16, 16, 16)

    def test_has_no_shortcut_projection_when_the_shape_is_unchanged(self):
        block = GroupNormResidualBlock(16, 16, stride=1, groups=4)

        # 3x3 conv 2 本 (16*16*9) と GroupNorm 2 本 (16 scale + 16 shift) だけ
        assert sum(p.numel() for p in block.parameters()) == 2 * (16 * 16 * 9 + 32)

    def test_adds_a_shortcut_projection_when_the_shape_changes(self):
        block = GroupNormResidualBlock(16, 32, stride=2, groups=4)

        # 上記に加えて 1x1 conv (16*32) と GroupNorm (32 + 32)
        expected = 16 * 32 * 9 + 64 + 32 * 32 * 9 + 64 + 16 * 32 + 64
        assert sum(p.numel() for p in block.parameters()) == expected

    def test_output_does_not_depend_on_the_batch_size(self):
        torch.manual_seed(5)
        block = GroupNormResidualBlock(16, 32, stride=2, groups=4).eval()
        inputs = torch.randn(2, 16, 16, 16)

        alone = block(inputs[:1])
        batched = block(inputs)

        torch.testing.assert_close(alone, batched[:1])


class TestBuildGroupNormResidualStage:
    """同じ channel 数の block を重ねた 1 stage."""

    def test_only_the_first_block_changes_the_shape(self):
        stage = build_group_norm_residual_stage(
            16, 32, block_count=3, first_stride=2, groups=4
        )

        outputs = stage(torch.randn(2, 16, 16, 16))

        assert tuple(outputs.shape) == (2, 32, 8, 8)

    def test_stacks_the_requested_number_of_blocks(self):
        stage = build_group_norm_residual_stage(
            16, 16, block_count=3, first_stride=1, groups=4
        )

        assert len(stage) == 3

    @pytest.mark.parametrize("block_count", [0, -1])
    def test_rejects_a_non_positive_block_count(self, block_count: int):
        with pytest.raises(ValueError, match="block_count"):
            build_group_norm_residual_stage(
                16, 16, block_count=block_count, first_stride=1, groups=4
            )


class TestReplaceInvalidPixels:
    """Padding 領域を学習可能な画素値へ置き換える."""

    def test_keeps_valid_pixels_and_fills_invalid_ones(self):
        images = torch.ones(1, 3, 2, 2)
        mask = torch.tensor([[[[True, False], [True, False]]]])
        fill = torch.full((1, 3, 1, 1), -5.0)

        replaced = replace_invalid_pixels(images, mask, fill)

        assert bool((replaced[:, :, :, 0] == 1.0).all())
        assert bool((replaced[:, :, :, 1] == -5.0).all())

    def test_does_not_propagate_non_finite_values_from_invalid_pixels(self):
        images = torch.full((1, 1, 1, 2), float("nan"))
        images[0, 0, 0, 0] = 1.0
        mask = torch.tensor([[[[True, False]]]])
        fill = torch.zeros(1, 1, 1, 1)

        replaced = replace_invalid_pixels(images, mask, fill)

        assert bool(torch.isfinite(replaced).all())


class TestImageEncoderConfig:
    """Encoder 設定の整合検証と派生値."""

    def test_accepts_a_consistent_configuration(self):
        assert CONFIG.validate() is None

    def test_output_features_is_the_last_stage_channel_count(self):
        assert CONFIG.output_features == 32

    def test_total_stride_multiplies_stem_and_stage_strides(self):
        assert CONFIG.total_stride == 2 * 2 * 1 * 2

    def test_rejects_mismatched_stem_tuple_lengths(self):
        config = _with_stem_strides((2,))

        assert config.validate() == (
            "stem_channels と stem_strides の長さが一致しません: 2 と 1"
        )

    def test_rejects_mismatched_stage_tuple_lengths(self):
        config = ImageEncoderConfig(
            input_channels=3,
            stem_channels=(8,),
            stem_strides=(2,),
            stage_channels=(16, 32),
            stage_strides=(1,),
            blocks_per_stage=(1, 1),
            group_norm_groups=4,
        )

        reason = config.validate()

        assert reason is not None
        assert "blocks_per_stage" in reason

    def test_rejects_a_non_positive_channel_count(self):
        config = ImageEncoderConfig(
            input_channels=3,
            stem_channels=(0,),
            stem_strides=(2,),
            stage_channels=(16,),
            stage_strides=(1,),
            blocks_per_stage=(1,),
            group_norm_groups=4,
        )

        assert config.validate() == "channel 数は正の整数が必要です: 0"

    def test_rejects_channels_that_are_not_divisible_by_groups(self):
        config = ImageEncoderConfig(
            input_channels=3,
            stem_channels=(10,),
            stem_strides=(2,),
            stage_channels=(16,),
            stage_strides=(1,),
            blocks_per_stage=(1,),
            group_norm_groups=4,
        )

        reason = config.validate()

        assert reason is not None
        assert "group_norm_groups" in reason

    def test_rejects_a_non_positive_stride(self):
        config = _with_stem_strides((0, 2))

        assert config.validate() == "stride は正の整数が必要です: 0"

    def test_rejects_an_empty_stage(self):
        config = ImageEncoderConfig(
            input_channels=3,
            stem_channels=(8,),
            stem_strides=(2,),
            stage_channels=(),
            stage_strides=(),
            blocks_per_stage=(),
            group_norm_groups=4,
        )

        assert config.validate() == "stem と stage はそれぞれ 1 段以上が必要です"

    def test_rejects_a_non_positive_input_channel_count(self):
        config = ImageEncoderConfig(
            input_channels=0,
            stem_channels=(8,),
            stem_strides=(2,),
            stage_channels=(16,),
            stage_strides=(1,),
            blocks_per_stage=(1,),
            group_norm_groups=4,
        )

        assert config.validate() == "input_channels は正の整数が必要です: 0"

    def test_rejects_a_non_positive_block_count(self):
        config = ImageEncoderConfig(
            input_channels=3,
            stem_channels=(8,),
            stem_strides=(2,),
            stage_channels=(16,),
            stage_strides=(1,),
            blocks_per_stage=(0,),
            group_norm_groups=4,
        )

        assert config.validate() == "blocks_per_stage は正の整数が必要です: 0"


class TestImageEncoder:
    """可変サイズ画像を固定長 feature へ落とす encoder."""

    def test_rejects_an_invalid_configuration(self):
        config = _with_stem_strides((2,))

        with pytest.raises(ValueError, match="stem_strides"):
            ImageEncoder(config)

    def test_reports_the_output_feature_count(self):
        assert _encoder().output_features == CONFIG.output_features

    @pytest.mark.parametrize(("height", "width"), [(32, 32), (96, 64)])
    def test_output_shape_is_independent_of_the_input_size(
        self, height: int, width: int
    ):
        encoder = _encoder()

        features = encoder(_images(2, height=height, width=width))

        assert tuple(features.shape) == (2, CONFIG.output_features)

    def test_accepts_the_smallest_image_allowed_by_the_total_stride(self):
        encoder = _encoder()
        size = CONFIG.total_stride

        features = encoder(_images(1, height=size, width=size))

        assert tuple(features.shape) == (1, CONFIG.output_features)

    def test_an_all_valid_mask_matches_passing_no_mask(self):
        encoder = _encoder()
        images = _images(2)

        torch.testing.assert_close(encoder(images, _mask(images)), encoder(images))

    def test_accepts_an_entirely_invalid_mask(self):
        encoder = _encoder()
        images = _images(1)

        features = encoder(images, _mask(images, valid=False))

        assert bool(torch.isfinite(features).all())

    def test_output_does_not_depend_on_the_batch_size(self):
        encoder = _encoder()
        images = _images(2)

        torch.testing.assert_close(encoder(images[:1]), encoder(images)[:1])

    def test_training_and_evaluation_modes_agree(self):
        encoder = _encoder()
        images = _images(1)

        evaluated = encoder(images)
        encoder.train()
        trained = encoder(images)

        torch.testing.assert_close(trained, evaluated)

    def test_pixels_under_an_invalid_mask_do_not_change_the_output(self):
        encoder = _encoder()
        images = _images(1)
        mask = _mask(images)
        mask[:, :, :8, :] = False
        overwritten = images.clone()
        overwritten[:, :, :8, :] = 1234.0

        torch.testing.assert_close(encoder(overwritten, mask), encoder(images, mask))

    def test_the_padding_pixel_receives_a_gradient_from_invalid_regions(self):
        encoder = _encoder()
        images = _images(1)
        mask = _mask(images)
        mask[:, :, :8, :] = False

        encoder(images, mask).sum().backward()

        gradient = encoder.padding_pixel.grad
        assert gradient is not None
        assert float(gradient.abs().sum().item()) > 0.0

    def test_rejects_images_that_are_not_four_dimensional(self):
        encoder = _encoder()

        with pytest.raises(ValueError, match=r"\[B, C, H, W\]"):
            encoder(torch.randn(3, 32, 32))

    def test_rejects_images_with_an_unexpected_channel_count(self):
        encoder = _encoder()

        with pytest.raises(ValueError, match="channel"):
            encoder(torch.randn(1, 4, 32, 32))

    def test_rejects_a_mask_that_is_not_boolean(self):
        encoder = _encoder()
        images = _images(1)

        with pytest.raises(ValueError, match="bool"):
            encoder(images, _mask(images).to(torch.uint8))

    def test_rejects_a_mask_with_a_mismatched_shape(self):
        encoder = _encoder()
        images = _images(1)

        with pytest.raises(ValueError, match="valid pixel mask"):
            encoder(images, torch.ones(1, 3, 32, 32, dtype=torch.bool))


class TestExportedDynamicShapes:
    """``torch.export`` 後も高さ・幅が固定されない.

    mask 経路の shape 検査に ``int()`` が入ると、非 strict export が SymInt を
    example の解像度へ落とし、``dynamic_shapes`` の宣言が黙って無視される。

    ``strict=False`` を明示するのは、torch 側の既定が変わってもこの検出力を
    保つため。strict 経路（dynamo）では ``int()`` があっても特殊化されない。
    """

    def test_accepts_another_resolution_on_the_mask_path(self):
        encoder = _encoder()
        images = _images(2)
        dynamic = {
            0: torch.export.Dim.AUTO,
            2: torch.export.Dim.AUTO,
            3: torch.export.Dim.AUTO,
        }

        exported = torch.export.export(
            encoder,
            (images, _mask(images)),
            dynamic_shapes={"images": dynamic, "valid_pixel_mask": dynamic},
            strict=False,
        )
        other = _images(3, height=48, width=64)
        features = exported.module()(other, _mask(other))

        assert tuple(features.shape) == (3, CONFIG.output_features)

    def test_accepts_another_resolution_without_a_mask(self):
        encoder = _encoder()
        dynamic = {
            0: torch.export.Dim.AUTO,
            2: torch.export.Dim.AUTO,
            3: torch.export.Dim.AUTO,
        }

        exported = torch.export.export(
            encoder, (_images(2),), dynamic_shapes={"images": dynamic}, strict=False
        )
        features = exported.module()(_images(3, height=48, width=64))

        assert tuple(features.shape) == (3, CONFIG.output_features)
