"""Parameter 数と multiply-accumulate 数の実測の公開契約."""

import pytest
import torch
from torch import nn

from ml.model.blocks import ImageEncoder, ImageEncoderConfig
from ml.model.heads import (
    GaussianHeadConfig,
    GaussianImageRegressor,
    GaussianRegressionHead,
)
from ml.model.inspection import ModelSize

ENCODER_CONFIG = ImageEncoderConfig(
    input_channels=3,
    stem_channels=(8, 16),
    stem_strides=(2, 2),
    stage_channels=(16, 32),
    stage_strides=(1, 2),
    blocks_per_stage=(1, 1),
    group_norm_groups=4,
)


def _regressor() -> GaussianImageRegressor:
    torch.manual_seed(53)
    encoder = ImageEncoder(ENCODER_CONFIG)
    head = GaussianRegressionHead(
        GaussianHeadConfig(
            input_features=ENCODER_CONFIG.output_features, hidden_features=16
        )
    )
    return GaussianImageRegressor(encoder, head)


class TestModelSizeCountParameters:
    """Model が持つ parameter 要素数の数え上げ."""

    def test_counts_every_parameter_by_default(self):
        model = nn.Linear(4, 6)

        assert ModelSize.count_parameters(model) == 4 * 6 + 6

    def test_counts_only_trainable_parameters_when_requested(self):
        model = nn.Linear(4, 6)
        model.bias.requires_grad_(False)

        assert ModelSize.count_parameters(model, trainable_only=True) == 4 * 6

    def test_shrinks_after_freezing_a_submodule(self):
        model = _regressor()
        before = ModelSize.count_parameters(model, trainable_only=True)

        for parameter in model.parameters():
            parameter.requires_grad_(False)

        assert ModelSize.count_parameters(model) == before
        assert ModelSize.count_parameters(model, trainable_only=True) == 0


class TestModelSize:
    """実測した model の大きさ."""

    def test_reports_multiply_accumulate_in_giga_units(self):
        size = ModelSize(
            parameter_count=1,
            trainable_parameter_count=1,
            multiply_accumulate_count=2_500_000_000,
        )

        assert size.giga_multiply_accumulate == pytest.approx(2.5)


class TestModelSizeMeasure:
    """Forward hook で Conv2d と Linear の演算量を実測する."""

    def test_counts_a_single_convolution_by_hand(self):
        model = nn.Conv2d(3, 8, 3, stride=1, padding=1, bias=False)

        size = ModelSize.measure(model, [torch.randn(1, 3, 8, 8)])

        assert size.multiply_accumulate_count == 8 * 8 * 8 * 3 * 3 * 3

    def test_counts_a_single_linear_layer_by_hand(self):
        model = nn.Linear(4, 6)

        size = ModelSize.measure(model, [torch.randn(1, 4)])

        assert size.multiply_accumulate_count == 6 * 4

    def test_ignores_normalization_pooling_and_activation(self):
        convolution = nn.Conv2d(3, 8, 3, stride=1, padding=1, bias=False)
        model = nn.Sequential(
            convolution,
            nn.GroupNorm(4, 8),
            nn.ReLU(),
            nn.AdaptiveAvgPool2d((1, 1)),
        )

        example = torch.randn(1, 3, 8, 8)
        composed = ModelSize.measure(model, [example])
        bare = ModelSize.measure(convolution, [example])

        assert composed.multiply_accumulate_count == bare.multiply_accumulate_count

    def test_counts_grouped_convolutions_per_group(self):
        model = nn.Conv2d(4, 8, 3, stride=1, padding=1, groups=2, bias=False)

        size = ModelSize.measure(model, [torch.randn(1, 4, 8, 8)])

        assert size.multiply_accumulate_count == 8 * 8 * 8 * (4 // 2) * 3 * 3

    def test_reports_the_same_parameter_counts_as_count_parameters(self):
        model = _regressor()

        size = ModelSize.measure(model, [torch.randn(1, 3, 32, 32)])

        assert size.parameter_count == ModelSize.count_parameters(model)
        assert size.trainable_parameter_count == ModelSize.count_parameters(
            model, trainable_only=True
        )
        assert size.multiply_accumulate_count > 0

    def test_restores_the_training_mode(self):
        model = _regressor()
        model.train()

        ModelSize.measure(model, [torch.randn(1, 3, 32, 32)])

        assert model.training

    def test_leaves_no_hook_behind(self):
        model = nn.Conv2d(3, 8, 3, stride=1, padding=1, bias=False)
        example = torch.randn(1, 3, 8, 8)

        first = ModelSize.measure(model, [example])
        second = ModelSize.measure(model, [example])

        assert second.multiply_accumulate_count == first.multiply_accumulate_count

    @pytest.mark.parametrize("batch_size", [2, 0])
    def test_rejects_inputs_whose_batch_dimension_is_not_one(self, batch_size: int):
        model = nn.Linear(4, 6)

        with pytest.raises(ValueError, match="batch 次元"):
            ModelSize.measure(model, [torch.randn(batch_size, 4)])

    def test_rejects_an_empty_example_input_sequence(self):
        with pytest.raises(ValueError, match="1 個以上"):
            ModelSize.measure(nn.Linear(4, 6), [])
