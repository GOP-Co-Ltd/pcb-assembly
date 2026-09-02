from __future__ import annotations

import copy
import math
from collections import Counter
from typing import cast

import pytest
import torch
from torch import nn

from pcbasm.pasting.paste_volume.model import (
    PasteVolumeModelConfig,
    PasteVolumeResNet,
    model_parameter_count,
    validate_loss_inputs,
    validate_model_inputs,
    weighted_gaussian_nll,
)


def _model_inputs(
    *,
    batch_size: int = 1,
    height: int = 32,
    width: int = 32,
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    image = torch.randn(batch_size, 6, height, width)
    valid_mask = torch.ones(batch_size, 1, height, width, dtype=torch.bool)
    pixel_per_mm = torch.full((batch_size, 1), 24.0)
    return image, valid_mask, pixel_per_mm


class TestPasteVolumeModelConfig:
    def test_defaults_fix_the_v1_architecture(self):
        config = PasteVolumeModelConfig()

        assert config.family == "paste-volume-resnet-small-v1"
        assert config.input_channels == 6
        assert config.stem_channels == (24, 32, 48)
        assert config.stage_channels == (48, 96, 160)
        assert config.blocks_per_stage == (2, 2, 2)
        assert config.group_norm_groups == 8
        assert config.hidden_features == 128
        assert config.log_variance_min == -14.0
        assert config.log_variance_max == 5.0

    @pytest.mark.parametrize(
        ("factory", "message"),
        [
            (
                lambda: PasteVolumeModelConfig(family="another-model"),
                "unsupported model family",
            ),
            (
                lambda: PasteVolumeModelConfig(input_channels=3),
                "exactly 6 input channels",
            ),
            (
                lambda: PasteVolumeModelConfig(
                    stem_channels=cast(tuple[int, int, int], (24, 32))
                ),
                "exactly three encoder stages",
            ),
            (
                lambda: PasteVolumeModelConfig(blocks_per_stage=(2, 0, 2)),
                "three positive values",
            ),
            (
                lambda: PasteVolumeModelConfig(group_norm_groups=2),
                "one of 1, 4, or 8",
            ),
            (
                lambda: PasteVolumeModelConfig(
                    stem_channels=(24, 30, 48), group_norm_groups=8
                ),
                "divisible by GroupNorm groups",
            ),
            (
                lambda: PasteVolumeModelConfig(hidden_features=0),
                "hidden_features must be positive",
            ),
            (
                lambda: PasteVolumeModelConfig(
                    log_variance_min=5.0, log_variance_max=5.0
                ),
                "bounds must be increasing",
            ),
        ],
    )
    def test_invalid_configuration_is_rejected(self, factory, message):
        with pytest.raises(ValueError) as error:
            factory()

        assert message in str(error.value)


class TestPasteVolumeResNetArchitecture:
    def test_encoder_matches_the_v1_shape_contract(self):
        model = PasteVolumeResNet()
        convolutions = [
            module for module in model.modules() if isinstance(module, nn.Conv2d)
        ]
        stride_two_3x3 = [
            module
            for module in convolutions
            if module.kernel_size == (3, 3) and module.stride == (2, 2)
        ]
        stride_two_1x1 = [
            module
            for module in convolutions
            if module.kernel_size == (1, 1) and module.stride == (2, 2)
        ]
        linear_shapes = [
            (module.in_features, module.out_features)
            for module in model.modules()
            if isinstance(module, nn.Linear)
        ]
        pools = [
            module
            for module in model.modules()
            if isinstance(module, nn.AdaptiveAvgPool2d)
        ]

        assert len(convolutions) == 17
        assert all(module.bias is None for module in convolutions)
        assert [module.out_channels for module in stride_two_3x3] == [
            24,
            32,
            48,
            96,
            160,
        ]
        assert [module.out_channels for module in stride_two_1x1] == [96, 160]
        assert linear_shapes == [(161, 128), (128, 1), (128, 1)]
        assert len(pools) == 1
        assert pools[0].output_size == (1, 1)

    def test_encoder_uses_only_group_norm(self):
        model = PasteVolumeResNet()
        normalization_types = (
            nn.BatchNorm1d,
            nn.BatchNorm2d,
            nn.BatchNorm3d,
            nn.SyncBatchNorm,
            nn.InstanceNorm1d,
            nn.InstanceNorm2d,
            nn.InstanceNorm3d,
            nn.LayerNorm,
            nn.GroupNorm,
        )
        normalizations = [
            module
            for module in model.modules()
            if isinstance(module, normalization_types)
        ]

        assert len(normalizations) == 17
        assert all(type(module) is nn.GroupNorm for module in normalizations)
        assert Counter(module.num_channels for module in normalizations) == Counter(
            {24: 1, 32: 1, 48: 5, 96: 5, 160: 5}
        )
        assert all(module.num_groups == 8 for module in normalizations)
        assert all(module.eps == 1e-5 for module in normalizations)
        assert all(module.affine for module in normalizations)

    def test_parameter_count_stays_below_the_v1_budget(self):
        assert 0 < model_parameter_count(PasteVolumeResNet()) < 1_500_000


class TestPasteVolumeResNetForward:
    def test_output_shapes_units_and_bounds(self):
        torch.manual_seed(7)
        model = PasteVolumeResNet().eval()
        image, valid_mask, pixel_per_mm = _model_inputs(
            batch_size=3, height=64, width=96
        )

        with torch.inference_mode():
            mean, log_variance = model(image, valid_mask, pixel_per_mm)

        assert mean.shape == (3, 1)
        assert log_variance.shape == (3, 1)
        assert mean.dtype == torch.float32
        assert log_variance.dtype == torch.float32
        assert torch.all(torch.isfinite(mean))
        assert torch.all(torch.isfinite(log_variance))
        assert torch.all(mean > 0)
        assert torch.all(log_variance >= model.config.log_variance_min)
        assert torch.all(log_variance <= model.config.log_variance_max)

    @pytest.mark.parametrize(
        ("bounds", "expected"),
        [((-2.0, -1.0), -1.0), ((1.0, 2.0), 1.0)],
    )
    def test_log_variance_is_clamped_at_both_boundaries(self, bounds, expected):
        model = PasteVolumeResNet(
            PasteVolumeModelConfig(
                log_variance_min=bounds[0], log_variance_max=bounds[1]
            )
        ).eval()
        for parameter in model.parameters():
            nn.init.zeros_(parameter)
        image, valid_mask, pixel_per_mm = _model_inputs()

        with torch.inference_mode():
            mean, log_variance = model(image, valid_mask, pixel_per_mm)

        torch.testing.assert_close(mean, torch.full((1, 1), math.log(2.0)))
        torch.testing.assert_close(log_variance, torch.full((1, 1), expected))

    @pytest.mark.parametrize(("height", "width"), [(32, 32), (1024, 256)])
    def test_forward_supports_minimum_and_maximum_variable_shapes(self, height, width):
        model = PasteVolumeResNet().eval()
        image, valid_mask, pixel_per_mm = _model_inputs(height=height, width=width)

        with torch.inference_mode():
            mean, log_variance = model(image, valid_mask, pixel_per_mm)

        assert mean.shape == (1, 1)
        assert log_variance.shape == (1, 1)
        assert torch.all(torch.isfinite(mean))
        assert torch.all(torch.isfinite(log_variance))

    def test_batch_size_one_is_independent_of_mode_and_batch_statistics(self):
        torch.manual_seed(11)
        model = PasteVolumeResNet()
        image, valid_mask, pixel_per_mm = _model_inputs(
            batch_size=2, height=64, width=64
        )
        image[1].mul_(100.0).add_(50.0)
        pixel_per_mm[1] = 200.0

        model.train()
        with torch.inference_mode():
            train_single = model(image[:1], valid_mask[:1], pixel_per_mm[:1])
            train_batched = model(image, valid_mask, pixel_per_mm)
        model.eval()
        with torch.inference_mode():
            eval_single = model(image[:1], valid_mask[:1], pixel_per_mm[:1])

        for single_output, batched_output, evaluation_output in zip(
            train_single, train_batched, eval_single, strict=True
        ):
            torch.testing.assert_close(single_output, batched_output[:1])
            torch.testing.assert_close(single_output, evaluation_output)

    @pytest.mark.parametrize(
        ("case", "message"),
        [
            ("missing_batch", "image_6ch"),
            ("wrong_channels", "image_6ch"),
            ("wrong_mask_shape", "valid_pixel_mask"),
            ("wrong_scale_shape", "pixel_per_mm"),
            ("integer_image", "floating point"),
            ("integer_scale", "floating point"),
            ("non_boolean_mask", "valid_pixel_mask"),
        ],
    )
    def test_invalid_forward_contract_is_rejected(self, case, message):
        image, valid_mask, pixel_per_mm = _model_inputs()
        if case == "missing_batch":
            image = image[0]
        elif case == "wrong_channels":
            image = image[:, :5]
        elif case == "wrong_mask_shape":
            valid_mask = valid_mask[:, :, :-1]
        elif case == "wrong_scale_shape":
            pixel_per_mm = pixel_per_mm[:, 0]
        elif case == "integer_image":
            image = torch.zeros_like(image, dtype=torch.uint8)
        elif case == "integer_scale":
            pixel_per_mm = torch.ones_like(pixel_per_mm, dtype=torch.int64)
        elif case == "non_boolean_mask":
            valid_mask = valid_mask.to(dtype=torch.float32)

        with pytest.raises(ValueError) as error:
            PasteVolumeResNet()(image, valid_mask, pixel_per_mm)

        assert message in str(error.value)

    def test_padding_parameter_receives_gradient_from_invalid_pixels(self):
        torch.manual_seed(13)
        model = PasteVolumeResNet().train()
        image, valid_mask, pixel_per_mm = _model_inputs(height=32, width=32)
        valid_mask[:, :, :, 16:] = False

        mean, log_variance = model(image, valid_mask, pixel_per_mm)
        loss = weighted_gaussian_nll(
            mean,
            log_variance,
            torch.tensor([[0.5]]),
            torch.ones((1, 1)),
        )
        loss.backward()
        padding_parameters = [
            parameter
            for parameter in model.parameters()
            if tuple(parameter.shape) == (1, 6, 1, 1)
        ]

        assert len(padding_parameters) == 1
        padding_gradient = padding_parameters[0].grad
        assert padding_gradient is not None
        assert torch.all(torch.isfinite(padding_gradient))
        assert torch.count_nonzero(padding_gradient).item() > 0


class TestModelInputValidation:
    @pytest.mark.parametrize(
        "case",
        [
            "non_finite_image",
            "zero_scale",
            "negative_scale",
            "non_finite_scale",
            "no_valid_pixel",
        ],
    )
    def test_invalid_model_values_are_rejected(self, case):
        image, valid_mask, pixel_per_mm = _model_inputs()
        if case == "non_finite_image":
            image[0, 0, 0, 0] = float("nan")
        elif case == "zero_scale":
            pixel_per_mm[0, 0] = 0.0
        elif case == "negative_scale":
            pixel_per_mm[0, 0] = -1.0
        elif case == "non_finite_scale":
            pixel_per_mm[0, 0] = float("inf")
        elif case == "no_valid_pixel":
            valid_mask.fill_(False)

        with pytest.raises(ValueError):
            validate_model_inputs(image, valid_mask, pixel_per_mm)

    def test_boundary_values_pass_model_validation(self):
        image, valid_mask, pixel_per_mm = _model_inputs()
        pixel_per_mm[0, 0] = torch.finfo(torch.float32).tiny
        valid_mask.fill_(False)
        valid_mask[0, 0, 0, 0] = True

        validate_model_inputs(image, valid_mask, pixel_per_mm)


class TestWeightedGaussianNll:
    def test_matches_the_weight_normalized_formula(self):
        mean = torch.tensor([[1.0], [2.0]])
        log_variance = torch.tensor([[0.0], [math.log(4.0)]])
        target = torch.tensor([[3.0], [4.0]])
        weight = torch.tensor([[1.0], [3.0]])
        expected = (2.0 + 3.0 * 0.5 * (1.0 + math.log(4.0))) / 4.0

        validate_loss_inputs(mean, log_variance, target, weight)
        actual = weighted_gaussian_nll(mean, log_variance, target, weight)

        torch.testing.assert_close(actual, torch.tensor(expected))
        torch.testing.assert_close(
            weighted_gaussian_nll(mean, log_variance, target, weight * 10.0),
            actual,
        )

    def test_zero_weight_sample_does_not_contribute(self):
        mean = torch.tensor([[1.0], [1000.0]])
        log_variance = torch.zeros((2, 1))
        target = torch.tensor([[3.0], [1.0]])
        weight = torch.tensor([[1.0], [0.0]])

        validate_loss_inputs(mean, log_variance, target, weight)

        torch.testing.assert_close(
            weighted_gaussian_nll(mean, log_variance, target, weight),
            torch.tensor(2.0),
        )

    def test_mismatched_shapes_are_rejected(self):
        with pytest.raises(ValueError) as error:
            weighted_gaussian_nll(
                torch.ones((2, 1)),
                torch.zeros((2, 1)),
                torch.ones((2, 1)),
                torch.ones((2,)),
            )

        assert "shapes must match" in str(error.value)

    @pytest.mark.parametrize(
        "case",
        [
            "non_finite_mean",
            "non_finite_log_variance",
            "non_finite_target",
            "non_finite_weight",
            "negative_weight",
            "zero_weight_sum",
        ],
    )
    def test_invalid_loss_values_are_rejected(self, case):
        mean = torch.ones((2, 1))
        log_variance = torch.zeros((2, 1))
        target = torch.ones((2, 1))
        weight = torch.ones((2, 1))
        if case == "non_finite_mean":
            mean[0, 0] = float("nan")
        elif case == "non_finite_log_variance":
            log_variance[0, 0] = float("inf")
        elif case == "non_finite_target":
            target[0, 0] = float("nan")
        elif case == "non_finite_weight":
            weight[0, 0] = float("inf")
        elif case == "negative_weight":
            weight[0, 0] = -1.0
        elif case == "zero_weight_sum":
            weight.zero_()

        with pytest.raises(ValueError):
            validate_loss_inputs(mean, log_variance, target, weight)


class TestFineTuneTrainability:
    def test_pi_fine_tuning_updates_only_the_planned_layers(self):
        model = PasteVolumeResNet()

        model.set_fine_tune_trainable()

        parameter_states = {
            name: parameter.requires_grad
            for name, parameter in model.named_parameters()
        }
        expected_prefixes = (
            "_stages.2.",
            "_features.",
            "_mean_head.",
            "_log_variance_head.",
        )
        assert any(parameter_states.values())
        assert not all(parameter_states.values())
        assert parameter_states["_padding_pixel"]
        assert all(
            trainable
            == (name == "_padding_pixel" or name.startswith(expected_prefixes))
            for name, trainable in parameter_states.items()
        )

    def test_full_model_fine_tuning_enables_every_parameter(self):
        model = PasteVolumeResNet()
        model.set_fine_tune_trainable()

        model.set_fine_tune_trainable(full_model=True)

        assert all(parameter.requires_grad for parameter in model.parameters())


class TestTorchCompileParity:
    def test_real_inductor_forward_loss_and_all_gradients_match_eager(self):
        torch.manual_seed(17)
        eager_model = PasteVolumeResNet().train()
        compiled_source = copy.deepcopy(eager_model).train()
        compiled_model = torch.compile(
            compiled_source,
            backend="inductor",
            mode="default",
            fullgraph=False,
            dynamic=None,
        )
        image, valid_mask, pixel_per_mm = _model_inputs(
            batch_size=2, height=32, width=32
        )
        valid_mask[:, :, :, 24:] = False
        target = torch.tensor([[0.5], [1.25]])
        weight = torch.tensor([[1.0], [0.25]])

        eager_mean, eager_log_variance = eager_model(image, valid_mask, pixel_per_mm)
        eager_loss = weighted_gaussian_nll(
            eager_mean, eager_log_variance, target, weight
        )
        eager_loss.backward()

        compiled_mean, compiled_log_variance = compiled_model(
            image, valid_mask, pixel_per_mm
        )
        compiled_loss = weighted_gaussian_nll(
            compiled_mean, compiled_log_variance, target, weight
        )
        compiled_loss.backward()

        torch.testing.assert_close(compiled_mean, eager_mean, rtol=1e-3, atol=1e-5)
        torch.testing.assert_close(
            compiled_log_variance, eager_log_variance, rtol=1e-3, atol=1e-5
        )
        torch.testing.assert_close(compiled_loss, eager_loss, rtol=1e-3, atol=1e-5)

        eager_gradients = {
            name: parameter.grad
            for name, parameter in eager_model.named_parameters()
            if parameter.requires_grad
        }
        compiled_gradients = {
            name: parameter.grad
            for name, parameter in compiled_source.named_parameters()
            if parameter.requires_grad
        }
        assert eager_gradients.keys() == compiled_gradients.keys()
        assert all(gradient is not None for gradient in eager_gradients.values())
        assert all(gradient is not None for gradient in compiled_gradients.values())
        for name in eager_gradients:
            eager_gradient = eager_gradients[name]
            compiled_gradient = compiled_gradients[name]
            assert eager_gradient is not None
            assert compiled_gradient is not None
            torch.testing.assert_close(
                compiled_gradient,
                eager_gradient,
                rtol=1e-3,
                atol=1e-5,
            )
