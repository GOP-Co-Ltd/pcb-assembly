"""塗布量推定 v1 encoder の公開契約.

仕様書 §2 が固定した諸元（parameter 数、総 stride、GMAC、fine-tune 範囲）を実測で 留める。

「死なない」型の検査には、同じ観測器が実際に死を見つけられることの自己検査を対にする。
"""

from __future__ import annotations

from typing import cast

import pytest
import torch
from torch import Tensor, nn

from ml.data.image import ImageConstraints
from ml.model.inspection import ModelSize
from ml.model.multiview import MultiViewGaussianRegressor
from ml.paste_volume.model import (
    CONDITIONING_FEATURES,
    INPUT_CHANNELS,
    PasteVolumeModelConfig,
    apply_fine_tune_freeze,
    build_paste_volume_model,
    measure_paste_volume_model,
)

# 仕様書 §2 の確定値。config の既定を変えたらここも動くので、二重に書く意味がある
DOCUMENTED_PARAMETER_COUNT = 395_048
DOCUMENTED_TOTAL_STRIDE = 8
DOCUMENTED_OUTPUT_FEATURES = 96

# 実データの crop 寸法と、点塗布 crop の上限
SMALL_IMAGE_SIZE = 53
LARGE_IMAGE_SIZE = 159
VIEW_COUNT = 5

# 仕様書 §2「GMAC は 1 view あたり 0.032 / 0.261、5 view なら 0.16 / 1.31」
DOCUMENTED_SMALL_GIGA_MULTIPLY_ACCUMULATE = 0.16
DOCUMENTED_LARGE_GIGA_MULTIPLY_ACCUMULATE = 1.31

# 仕様書 §2「512x512 入力で 1.5 GMAC 以下」
GIGA_MULTIPLY_ACCUMULATE_BUDGET = 1.5

# 仕様書 §2 申し送り 2 が 0/100 と報告した実測を、こちらでも同じ規模で測り直す
INITIALISATION_SEED_COUNT = 100

# `apply_fine_tune_freeze` が凍結する parameter 名。state_dict のキーは checkpoint と
# export の公開契約なので、集合ごと固定してよい
FROZEN_PARAMETER_NAMES = (
    "_encoder._encoder._stem.0.0.weight",
    "_encoder._encoder._stem.0.1.weight",
    "_encoder._encoder._stem.0.1.bias",
    "_encoder._encoder._stem.1.0.weight",
    "_encoder._encoder._stem.1.1.weight",
    "_encoder._encoder._stem.1.1.bias",
    "_encoder._encoder._stages.0.0._entry.0.weight",
    "_encoder._encoder._stages.0.0._entry.1.weight",
    "_encoder._encoder._stages.0.0._entry.1.bias",
    "_encoder._encoder._stages.0.0._convolution.weight",
    "_encoder._encoder._stages.0.0._normalization.weight",
    "_encoder._encoder._stages.0.0._normalization.bias",
    "_encoder._encoder._stages.0.0._shortcut.0.weight",
    "_encoder._encoder._stages.0.0._shortcut.1.weight",
    "_encoder._encoder._stages.0.0._shortcut.1.bias",
    "_encoder._encoder._stages.0.1._entry.0.weight",
    "_encoder._encoder._stages.0.1._entry.1.weight",
    "_encoder._encoder._stages.0.1._entry.1.bias",
    "_encoder._encoder._stages.0.1._convolution.weight",
    "_encoder._encoder._stages.0.1._normalization.weight",
    "_encoder._encoder._stages.0.1._normalization.bias",
)

# 凍結後に残る parameter。stem 8,320 と stage 1 の 78,048 を引いた数
TRAINABLE_PARAMETER_COUNT_AFTER_FREEZE = 308_680

PADDING_PIXEL_NAME = "_encoder._encoder._padding_pixel"
TRUNK_BIAS_NAME = "_head._trunk.0.bias"


def _model(seed: int = 0) -> MultiViewGaussianRegressor:
    torch.manual_seed(seed)
    model, error = build_paste_volume_model(PasteVolumeModelConfig())
    assert error is None
    assert model is not None
    return model


def _inputs(
    *, sample_count: int = 4, size: int = SMALL_IMAGE_SIZE, seed: int = 7
) -> tuple[Tensor, Tensor, Tensor]:
    """Sample ごとに違う、全 0 でない入力を返す.

    Model の初期化 seed と独立した生成器を使う。

    100 seed の間で入力を固定しないと、観測している差が初期化由来か入力由来か 分けられない。
    """

    generator = torch.Generator().manual_seed(seed)
    images = torch.rand(
        (sample_count, VIEW_COUNT, INPUT_CHANNELS, size, size), generator=generator
    )
    valid_pixel_mask = torch.ones(
        (sample_count, VIEW_COUNT, 1, size, size), dtype=torch.bool
    )
    conditioning = torch.rand(
        (sample_count, CONDITIONING_FEATURES), generator=generator
    )
    return images, valid_pixel_mask, conditioning


def _head_output_spread(
    model: MultiViewGaussianRegressor, inputs: tuple[Tensor, Tensor, Tensor]
) -> float:
    """Head の出力が sample 間でどれだけ動くかを返す.

    観測に使うのは log 分散側。

    平均側は weight を 0 から初期化しているので、学習前は必ず bias 一定になり、 trunk
    が生きているかどうかを映さない。
    """

    model.eval()
    with torch.no_grad():
        _mean, log_variance = model(*inputs)
    return float(log_variance.max().item() - log_variance.min().item())


def _kill_the_trunk(model: MultiViewGaussianRegressor) -> None:
    """Trunk の ReLU が全 sample・全 unit で 0 になるようにする."""

    state = model.state_dict()
    state[TRUNK_BIAS_NAME] = torch.full_like(state[TRUNK_BIAS_NAME], -1e6)
    model.load_state_dict(state)


def _backward_once(model: MultiViewGaussianRegressor) -> None:
    mean, log_variance = model(*_inputs(sample_count=2))
    (mean.sum() + log_variance.sum()).backward()


class _RenamedRegressor(nn.Module):
    """属性名が ``ml.model`` と違う model の代役."""

    def __init__(self) -> None:
        super().__init__()
        self.encoder = nn.Conv2d(INPUT_CHANNELS, 8, 3)


class TestPasteVolumeModelConfig:
    """既定値が仕様書 §2 の確定値そのものであること."""

    def test_maps_the_documented_encoder_shape(self):
        encoder = PasteVolumeModelConfig().encoder_config()

        assert encoder.input_channels == INPUT_CHANNELS
        assert encoder.total_stride == DOCUMENTED_TOTAL_STRIDE
        assert encoder.output_features == DOCUMENTED_OUTPUT_FEATURES

    def test_feeds_the_head_with_the_pooled_features_and_one_condition(self):
        head = PasteVolumeModelConfig().head_config()

        assert head.input_features == DOCUMENTED_OUTPUT_FEATURES
        assert head.conditioning_features == CONDITIONING_FEATURES
        assert head.hidden_features == 128

    def test_starts_the_mean_bias_inside_the_measured_volume_scale(self):
        assert PasteVolumeModelConfig().mean_bias_initial == pytest.approx(0.15)

    def test_accepts_the_default_image_constraints(self):
        assert (
            PasteVolumeModelConfig().validate_for_constraints(ImageConstraints())
            is None
        )

    def test_reports_a_total_stride_the_smallest_input_cannot_afford(self):
        config = PasteVolumeModelConfig(stage_strides=(2, 4))

        error = config.validate_for_constraints(ImageConstraints())

        assert error is not None
        assert "total_stride" in error

    def test_reports_a_channel_count_the_group_norm_cannot_divide(self):
        error = PasteVolumeModelConfig(stage_channels=(48, 100)).validate()

        assert error is not None
        assert "group_norm_groups" in error

    def test_reports_a_mean_bias_that_would_kill_the_mean_head(self):
        error = PasteVolumeModelConfig(mean_bias_initial=0.0).validate()

        assert error is not None
        assert "mean_bias_initial" in error


class TestBuildPasteVolumeModel:
    """設定から model を組む経路."""

    def test_builds_the_documented_model_from_the_defaults(self):
        model, error = build_paste_volume_model(PasteVolumeModelConfig())

        assert error is None
        assert isinstance(model, MultiViewGaussianRegressor)

    def test_reports_the_reason_instead_of_building_an_invalid_model(self):
        model, error = build_paste_volume_model(
            PasteVolumeModelConfig(hidden_features=0)
        )

        assert model is None
        assert error is not None
        assert "hidden_features" in error

    def test_starts_every_sample_at_the_configured_mean_bias(self):
        """学習前の平均出力が sample に依らず ``mean_bias_initial`` になること.

        平均線形層の weight を 0 から始めていることの観測点。

        weight が 0 でないと前活性が sample ごとにばらけ、bias を負側へ飲み込んだ sample
        の平均勾配が初期化時点で切れる。
        """

        model = _model()
        model.eval()

        with torch.no_grad():
            mean, _log_variance = model(*_inputs())

        assert mean.shape == (4, 1)
        assert mean.flatten().tolist() == pytest.approx([0.15] * 4)


class TestMeasurePasteVolumeModel:
    """Parameter 数と演算量の実測."""

    def test_counts_exactly_the_documented_parameter_count(self):
        size = measure_paste_volume_model(
            _model(), height=SMALL_IMAGE_SIZE, width=SMALL_IMAGE_SIZE, view_count=1
        )

        assert size.parameter_count == DOCUMENTED_PARAMETER_COUNT

    @pytest.mark.parametrize(
        ("image_size", "documented"),
        [
            (SMALL_IMAGE_SIZE, DOCUMENTED_SMALL_GIGA_MULTIPLY_ACCUMULATE),
            (LARGE_IMAGE_SIZE, DOCUMENTED_LARGE_GIGA_MULTIPLY_ACCUMULATE),
        ],
    )
    def test_matches_the_documented_five_view_computation(
        self, image_size: int, documented: float
    ):
        size = measure_paste_volume_model(
            _model(), height=image_size, width=image_size, view_count=VIEW_COUNT
        )

        assert size.giga_multiply_accumulate == pytest.approx(documented, rel=0.01)

    def test_stays_within_the_computation_budget_at_the_largest_crop(self):
        size = measure_paste_volume_model(
            _model(),
            height=LARGE_IMAGE_SIZE,
            width=LARGE_IMAGE_SIZE,
            view_count=VIEW_COUNT,
        )

        assert size.giga_multiply_accumulate < GIGA_MULTIPLY_ACCUMULATE_BUDGET


class TestTrunkActivation:
    """Head の hidden 層が初期化直後に死なないこと（仕様書 §2 申し送り 2）."""

    def test_keeps_the_head_moving_between_samples_for_every_seed(self):
        inputs = _inputs()

        dead = [
            seed
            for seed in range(INITIALISATION_SEED_COUNT)
            if _head_output_spread(_model(seed), inputs) == 0.0
        ]

        assert dead == []

    def test_the_same_observation_finds_a_trunk_that_is_dead(self):
        """観測器が死を見つけられることの自己検査.

        比較が壊れると上の検査は常に緑になるので、同じ関数で反対側を測る。
        """

        model = _model()
        _kill_the_trunk(model)

        assert _head_output_spread(model, _inputs()) == 0.0


class TestApplyFineTuneFreeze:
    """Raspberry Pi 既定 fine-tune の更新範囲."""

    def test_freezes_the_stem_and_every_stage_but_the_last(self):
        frozen = apply_fine_tune_freeze(_model())

        assert set(frozen) == set(FROZEN_PARAMETER_NAMES)

    def test_leaves_the_last_stage_the_head_and_the_padding_pixel_trainable(self):
        model = _model()

        frozen = frozenset(apply_fine_tune_freeze(model))

        trainable = {
            name
            for name, parameter in model.named_parameters()
            if parameter.requires_grad
        }
        assert trainable == {name for name, _ in model.named_parameters()} - frozen
        assert PADDING_PIXEL_NAME in trainable
        assert (
            ModelSize.count_parameters(model, trainable_only=True)
            == TRAINABLE_PARAMETER_COUNT_AFTER_FREEZE
        )

    def test_keeps_the_learnable_padding_pixel_trainable(self):
        model = _model()

        apply_fine_tune_freeze(model)

        padding_pixel = dict(model.named_parameters())[PADDING_PIXEL_NAME]
        assert padding_pixel.requires_grad

    def test_stops_the_gradient_at_the_frozen_parameters(self):
        model = _model()
        apply_fine_tune_freeze(model)

        _backward_once(model)

        gradients = {
            name: parameter.grad for name, parameter in model.named_parameters()
        }
        assert [
            name for name in FROZEN_PARAMETER_NAMES if gradients[name] is not None
        ] == []

    def test_the_same_observation_sees_gradients_without_the_freeze(self):
        """凍結しない model では同じ観測点に勾配が来ることの自己検査."""

        model = _model()

        _backward_once(model)

        gradients = {
            name: parameter.grad for name, parameter in model.named_parameters()
        }
        assert [
            name for name in FROZEN_PARAMETER_NAMES if gradients[name] is None
        ] == []

    def test_rejects_a_model_whose_parameter_names_do_not_match(self):
        """名前が変わったときに「1 つも凍結しない freeze」が成功しないこと."""

        with pytest.raises(ValueError, match="residual stage"):
            apply_fine_tune_freeze(
                cast(MultiViewGaussianRegressor, _RenamedRegressor())
            )
