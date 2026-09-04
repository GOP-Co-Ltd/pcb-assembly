"""Gaussian 回帰 head と合成 model の公開契約."""

import attrs
import pytest
import torch

from ml.model.blocks import ImageEncoder, ImageEncoderConfig
from ml.model.heads import (
    GaussianHeadConfig,
    GaussianImageRegressor,
    GaussianRegressionHead,
)
from ml.model.loss import weighted_gaussian_negative_log_likelihood

ENCODER_CONFIG = ImageEncoderConfig(
    input_channels=3,
    stem_channels=(8, 16),
    stem_strides=(2, 2),
    stage_channels=(16, 32),
    stage_strides=(1, 2),
    blocks_per_stage=(1, 1),
    group_norm_groups=4,
)
HEAD_CONFIG = GaussianHeadConfig(
    input_features=ENCODER_CONFIG.output_features,
    conditioning_features=0,
    hidden_features=16,
)


def _head(**overrides) -> GaussianRegressionHead:
    torch.manual_seed(17)
    return GaussianRegressionHead(attrs.evolve(HEAD_CONFIG, **overrides)).eval()


def _regressor(**overrides) -> GaussianImageRegressor:
    torch.manual_seed(23)
    encoder = ImageEncoder(ENCODER_CONFIG)
    head = GaussianRegressionHead(attrs.evolve(HEAD_CONFIG, **overrides))
    return GaussianImageRegressor(encoder, head).eval()


def _features(count: int = 4) -> torch.Tensor:
    generator = torch.Generator().manual_seed(29)
    return torch.randn((count, HEAD_CONFIG.input_features), generator=generator)


class TestGaussianHeadConfig:
    """Head 設定の整合検証."""

    def test_accepts_a_consistent_configuration(self):
        assert HEAD_CONFIG.validate() is None

    def test_rejects_a_log_variance_range_that_is_not_increasing(self):
        config = attrs.evolve(
            HEAD_CONFIG, log_variance_minimum=5.0, log_variance_maximum=5.0
        )

        reason = config.validate()

        assert reason is not None
        assert "log_variance_maximum" in reason

    @pytest.mark.parametrize("hidden_features", [0, -1])
    def test_rejects_non_positive_hidden_features(self, hidden_features: int):
        config = attrs.evolve(HEAD_CONFIG, hidden_features=hidden_features)

        assert config.validate() == (
            f"hidden_features は正の整数が必要です: {hidden_features}"
        )

    def test_rejects_non_positive_input_features(self):
        config = attrs.evolve(HEAD_CONFIG, input_features=0)

        assert config.validate() == "input_features は正の整数が必要です: 0"

    def test_rejects_negative_conditioning_features(self):
        config = attrs.evolve(HEAD_CONFIG, conditioning_features=-1)

        reason = config.validate()

        assert reason is not None
        assert "conditioning_features" in reason

    def test_rejects_a_non_finite_log_variance_bound(self):
        config = attrs.evolve(HEAD_CONFIG, log_variance_minimum=float("-inf"))

        assert config.validate() == "log_variance_minimum は有限値が必要です: -inf"


class TestGaussianRegressionHead:
    """Pooled feature から平均と log 分散を出す head."""

    def test_rejects_an_invalid_configuration(self):
        with pytest.raises(ValueError, match="hidden_features"):
            GaussianRegressionHead(attrs.evolve(HEAD_CONFIG, hidden_features=0))

    def test_reports_its_input_and_conditioning_dimensions(self):
        head = _head(conditioning_features=2)

        assert head.input_features == HEAD_CONFIG.input_features
        assert head.conditioning_features == 2

    def test_returns_a_mean_and_a_log_variance_per_sample(self):
        mean, log_variance = _head()(_features())

        assert tuple(mean.shape) == (4, 1)
        assert tuple(log_variance.shape) == (4, 1)

    def test_mean_is_positive(self):
        mean, _ = _head()(_features())

        assert bool((mean > 0).all())

    def test_mean_stays_non_negative_and_finite_for_extreme_features(self):
        # Softplus は数学的には正だが、極端に負の入力では float32 で 0 へ丸まる。
        # 相対誤差を出す評価側は mean <= 0 の sample を無効として除外する。
        mean, _ = _head()(_features() * 1e6)

        assert bool((mean >= 0).all())
        assert bool(torch.isfinite(mean).all())

    @pytest.mark.parametrize("magnitude", [1.0, 1e6])
    def test_log_variance_stays_inside_the_configured_range(self, magnitude: float):
        head = _head(log_variance_minimum=-3.0, log_variance_maximum=2.0)

        _, log_variance = head(_features() * magnitude)

        assert bool((log_variance >= -3.0).all())
        assert bool((log_variance <= 2.0).all())

    def test_conditioning_changes_the_prediction(self):
        head = _head(conditioning_features=2)
        features = _features()
        conditioning = torch.zeros(4, 2)

        baseline, _ = head(features, conditioning)
        shifted, _ = head(features, conditioning + 1.0)

        assert not bool(torch.allclose(baseline, shifted))

    def test_rejects_features_that_are_not_two_dimensional(self):
        with pytest.raises(ValueError, match=r"\[B, F\]"):
            _head()(torch.randn(4))

    def test_rejects_features_with_an_unexpected_dimension(self):
        with pytest.raises(ValueError, match="features の次元数"):
            _head()(torch.randn(4, HEAD_CONFIG.input_features + 1))

    def test_rejects_a_missing_conditioning_tensor(self):
        with pytest.raises(ValueError, match="conditioning が必要です"):
            _head(conditioning_features=2)(_features())

    def test_rejects_a_conditioning_tensor_with_an_unexpected_dimension(self):
        with pytest.raises(ValueError, match="conditioning の次元数"):
            _head(conditioning_features=2)(_features(), torch.zeros(4, 3))

    def test_rejects_a_conditioning_tensor_that_is_not_two_dimensional(self):
        with pytest.raises(ValueError, match=r"\[B, K\]"):
            _head(conditioning_features=2)(_features(), torch.zeros(4))

    def test_rejects_a_conditioning_tensor_with_a_mismatched_batch_size(self):
        with pytest.raises(ValueError, match="batch size"):
            _head(conditioning_features=2)(_features(), torch.zeros(3, 2))

    def test_rejects_a_conditioning_tensor_with_a_mismatched_dtype(self):
        with pytest.raises(ValueError, match="dtype"):
            _head(conditioning_features=2)(
                _features(), torch.zeros(4, 2, dtype=torch.float64)
            )


class TestGaussianImageRegressor:
    """Encoder と head をつないだ単一 model."""

    def test_rejects_a_head_that_does_not_match_the_encoder(self):
        encoder = ImageEncoder(ENCODER_CONFIG)
        head = GaussianRegressionHead(
            attrs.evolve(HEAD_CONFIG, input_features=ENCODER_CONFIG.output_features + 8)
        )

        with pytest.raises(ValueError, match="input_features"):
            GaussianImageRegressor(encoder, head)

    def test_predicts_a_mean_and_a_log_variance_from_images(self):
        mean, log_variance = _regressor()(torch.randn(2, 3, 32, 32))

        assert tuple(mean.shape) == (2, 1)
        assert tuple(log_variance.shape) == (2, 1)
        assert bool((mean > 0).all())

    def test_accepts_a_mask_and_conditioning_together(self):
        regressor = _regressor(conditioning_features=1)
        images = torch.randn(2, 3, 32, 32)
        mask = torch.ones(2, 1, 32, 32, dtype=torch.bool)

        mean, _ = regressor(images, mask, torch.zeros(2, 1))

        assert tuple(mean.shape) == (2, 1)


class TestGaussianImageRegressorTraining:
    """勾配が model 全体へ流れ、少数 sample を過学習できる."""

    def test_overfits_a_handful_of_samples(self):
        # 乱数は _regressor() の manual_seed が決めるので、ここでは種を撒かない
        regressor = _regressor()
        regressor.train()
        images = torch.randn(4, 3, 16, 16)
        target = torch.tensor([[0.5], [1.0], [2.0], [4.0]])
        weight = torch.ones_like(target)
        optimizer = torch.optim.Adam(regressor.parameters(), lr=0.02)

        error_history: list[float] = []
        for _ in range(300):
            mean, log_variance = regressor(images)
            loss = weighted_gaussian_negative_log_likelihood(
                mean, log_variance, target, weight
            )
            error_history.append(float((mean - target).abs().mean().item()))
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

        # Adam の軌跡は終盤で跳ねるので、最終値ではなく到達した最良値を見る
        # 種を 6 通り変えた実測の最悪比は 0.029。3 倍以上の余裕を残して 0.1 を閾値にする
        assert min(error_history) < error_history[0] * 0.1
