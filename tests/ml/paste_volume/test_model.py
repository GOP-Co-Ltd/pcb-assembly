"""塗布量推定 v1 encoder の公開契約.

仕様書 §2 が固定した諸元を実測で留める。

対象は parameter 数・総 stride・GMAC・clamp 範囲・fine-tune 範囲。

「死なない」型の検査には自己検査を対にする。

同じ観測器が実際に死を見つけられることを、反対側で 1 度示す。
"""

from __future__ import annotations

from pathlib import Path
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
    MODEL_FAMILY,
    PasteVolumeModelConfig,
    apply_fine_tune_freeze,
    build_paste_volume_model,
    measure_paste_volume_model,
)
from tests.ml.paste_volume.helpers import (
    PADDING_PIXEL_NAME,
    STEM_CONVOLUTION_NAME,
    paste_volume_model,
)

# 仕様書 §2 の確定値。config の既定を変えたらここも動くので、二重に書く意味がある
DOCUMENTED_PARAMETER_COUNT = 395_048
DOCUMENTED_TOTAL_STRIDE = 8
DOCUMENTED_OUTPUT_FEATURES = 96

# 仕様書 §2 が固定した識別子。export manifest と Optuna study の突き合わせ鍵
DOCUMENTED_MODEL_FAMILY = "paste-volume-resnet-small-v1"

# 仕様書 §2 の clamp 範囲 log_variance_volume_ul2 = clamp(raw_logvar, -14, 5)。
# parameter 数も GMAC も total_stride も変えないので、ここだけが観測点になる
DOCUMENTED_LOG_VARIANCE_MINIMUM = -14.0
DOCUMENTED_LOG_VARIANCE_MAXIMUM = 5.0

# 実データの crop 寸法と、点塗布 crop の上限
SMALL_IMAGE_SIZE = 53
LARGE_IMAGE_SIZE = 159
VIEW_COUNT = 5

# 仕様書 §2「GMAC は 1 view あたり 0.032 / 0.261、5 view なら 0.16 / 1.31」
DOCUMENTED_SMALL_GIGA_MULTIPLY_ACCUMULATE = 0.16
DOCUMENTED_LARGE_GIGA_MULTIPLY_ACCUMULATE = 1.31

# 仕様書 §2 の計算量上限。orchestrator 裁定で、gate は点塗布 crop の上限
# （159px x 5 view）で測ると決めた。仕様書 §2 の「512x512 入力で 1.5 GMAC 以下」は
# 多視点化以前の単一 view 時代の記述で、実測は 512x512x1view で 2.677027
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

TRUNK_BIAS_NAME = "_head._trunk.0.bias"


def _inputs(
    *, sample_count: int = 4, size: int = SMALL_IMAGE_SIZE, seed: int = 7
) -> tuple[Tensor, Tensor, Tensor]:
    """Sample ごとに違う、全 0 でない入力を返す.

    Model の初期化 seed と独立した生成器を使う。

    100 seed の間で入力を固定しないと、観測した差の出所が分けられない。

    初期化由来か入力由来かを切り分けられなくなる。
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

    平均側は weight を 0 から初期化しているので学習前は必ず bias 一定になる。

    その値は trunk が生きているかどうかを映さない。
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


class _StubEncoderBody(nn.Module):
    """``freeze`` が探す 3 部位のうち、指定したものだけを持つ中身.

    ``_stages`` は ``_stages.<i>.`` の形にする。stage 番号を数値で取り出す経路まで
    通したいため。
    """

    def __init__(self, *, stem: bool, stages: bool, padding_pixel: bool) -> None:
        super().__init__()
        if stem:
            self._stem = nn.Sequential(nn.Conv2d(INPUT_CHANNELS, 8, 3))
        if stages:
            self._stages = nn.Sequential(nn.Conv2d(8, 8, 3))
        if padding_pixel:
            self._padding_pixel = nn.Parameter(torch.zeros(INPUT_CHANNELS))


class _StubInnerEncoder(nn.Module):
    def __init__(self, body: nn.Module) -> None:
        super().__init__()
        self._encoder = body


class _StubRegressor(nn.Module):
    """部位の一部が欠けた model の代役.

    ``ml.model`` 側の属性名が変わった状況を、名前だけで再現する。
    """

    def __init__(
        self, *, stem: bool = True, stages: bool = True, padding_pixel: bool = True
    ) -> None:
        super().__init__()
        self._encoder = _StubInnerEncoder(
            _StubEncoderBody(stem=stem, stages=stages, padding_pixel=padding_pixel)
        )


class TestModelFamily:
    """Model family の識別子.

    export manifest と Optuna study の突き合わせ鍵なので、黙って変わると過去 run との対応が切れる。
    """

    def test_names_the_documented_model_family(self):
        assert MODEL_FAMILY == DOCUMENTED_MODEL_FAMILY


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

    def test_clamps_the_log_variance_to_the_documented_window(self):
        """Clamp 範囲が仕様書 §2 の確定値であること.

        blank の負の対数尤度が下限へ張り付く挙動は、この 2 値そのものに依存する。

        parameter 数も GMAC も total_stride も変わらないので、他の検査は素通りする。
        """

        head = PasteVolumeModelConfig().head_config()

        assert head.log_variance_minimum == DOCUMENTED_LOG_VARIANCE_MINIMUM
        assert head.log_variance_maximum == DOCUMENTED_LOG_VARIANCE_MAXIMUM

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

    def test_leaves_every_parameter_trainable_even_when_fine_tuning(self):
        """``fine_tune`` を立てても build は 1 つも凍結しないこと.

        凍結の適用は entrypoint 側の責務で、build → load → freeze の順を持つ。

        build が先に凍結すると、順番の所有者が 2 つに分かれる。
        """

        model, error = build_paste_volume_model(PasteVolumeModelConfig(fine_tune=True))

        assert error is None
        assert model is not None
        assert [
            name
            for name, parameter in model.named_parameters()
            if not parameter.requires_grad
        ] == []

    def test_does_not_read_the_initial_weights(self, tmp_path: Path):
        """``initial_weights`` の path を build が開かないこと.

        存在しない path でも成功するのが、読み込みを entrypoint へ寄せた契約。
        """

        model, error = build_paste_volume_model(
            PasteVolumeModelConfig(initial_weights=tmp_path / "absent.pt")
        )

        assert error is None
        assert model is not None

    def test_starts_every_sample_at_the_configured_mean_bias(self):
        """学習前の平均出力が sample に依らず ``mean_bias_initial`` になること.

        平均線形層の weight を 0 から始めていることの観測点。

        weight が 0 でないと前活性が sample ごとにばらけ、bias を負側へ飲み込んだ sample
        の平均勾配が初期化時点で切れる。
        """

        model = paste_volume_model()
        model.eval()

        with torch.no_grad():
            mean, _log_variance = model(*_inputs())

        assert mean.shape == (4, 1)
        assert mean.flatten().tolist() == pytest.approx([0.15] * 4)


class TestMeasurePasteVolumeModel:
    """Parameter 数と演算量の実測."""

    def test_counts_exactly_the_documented_parameter_count(self):
        size = measure_paste_volume_model(
            paste_volume_model(),
            height=SMALL_IMAGE_SIZE,
            width=SMALL_IMAGE_SIZE,
            view_count=1,
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
            paste_volume_model(),
            height=image_size,
            width=image_size,
            view_count=VIEW_COUNT,
        )

        assert size.giga_multiply_accumulate == pytest.approx(documented, rel=0.01)

    def test_stays_within_the_computation_budget_at_the_largest_crop(self):
        """点塗布 crop の上限で仕様書 §2 の 1.5 GMAC を下回ること.

        測る shape は 159px x 5 view（orchestrator 裁定）。

        ``ImageConstraints`` の 512 は前処理の契約上限で、点塗布 crop の上限ではない。
        """

        size = measure_paste_volume_model(
            paste_volume_model(),
            height=LARGE_IMAGE_SIZE,
            width=LARGE_IMAGE_SIZE,
            view_count=VIEW_COUNT,
        )

        assert size.giga_multiply_accumulate < GIGA_MULTIPLY_ACCUMULATE_BUDGET


class TestTrunkActivation:
    """Head の hidden 層が初期化直後に死なないこと（仕様書 §2 申し送り 2）.

    測るのは batch 全体・全 unit が死ぬ完全な死だけ。

    128 unit のうち 1 つでも生きていれば spread は 0 にならない。

    「ほぼ死んでいる」状態は 0/100 という記録に含まれない。
    """

    def test_keeps_the_head_moving_between_samples_for_every_seed(self):
        inputs = _inputs()

        dead = [
            seed
            for seed in range(INITIALISATION_SEED_COUNT)
            if _head_output_spread(paste_volume_model(seed), inputs) == 0.0
        ]

        assert dead == []

    def test_the_same_observation_finds_a_trunk_that_is_dead(self):
        """観測器が死を見つけられることの自己検査.

        比較が壊れると上の検査は常に緑になるので、同じ関数で反対側を測る。
        """

        model = paste_volume_model()
        _kill_the_trunk(model)

        assert _head_output_spread(model, _inputs()) == 0.0


class TestApplyFineTuneFreeze:
    """Raspberry Pi 既定 fine-tune の更新範囲."""

    def test_freezes_the_stem_and_every_stage_but_the_last(self):
        frozen = apply_fine_tune_freeze(paste_volume_model())

        assert set(frozen) == set(FROZEN_PARAMETER_NAMES)

    def test_leaves_the_last_stage_the_head_and_the_padding_pixel_trainable(self):
        model = paste_volume_model()

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
        model = paste_volume_model()

        apply_fine_tune_freeze(model)

        padding_pixel = dict(model.named_parameters())[PADDING_PIXEL_NAME]
        assert padding_pixel.requires_grad

    def test_stops_the_gradient_at_the_frozen_parameters(self):
        model = paste_volume_model()
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

        model = paste_volume_model()

        _backward_once(model)

        gradients = {
            name: parameter.grad for name, parameter in model.named_parameters()
        }
        assert [
            name for name in FROZEN_PARAMETER_NAMES if gradients[name] is None
        ] == []

    @pytest.mark.parametrize(
        ("missing", "reason"),
        [
            ("stages", "residual stage"),
            ("stem", "stem"),
            ("padding_pixel", "learnable padding pixel"),
        ],
    )
    def test_rejects_a_model_whose_parameter_names_do_not_match(
        self, missing: str, reason: str
    ):
        """名前が変わったときに「1 つも凍結しない freeze」が成功しないこと.

        番犬を 1 つずつ欠けさせ、3 本の理由文まで固定する。

        番犬そのものを消す変異が見えるようになるのがここの目的。
        """

        model = _StubRegressor(
            stem=missing != "stem",
            stages=missing != "stages",
            padding_pixel=missing != "padding_pixel",
        )

        with pytest.raises(ValueError, match=reason):
            apply_fine_tune_freeze(cast(MultiViewGaussianRegressor, model))
