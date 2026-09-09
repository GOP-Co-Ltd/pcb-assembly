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


def _head(*, seed: int = 17, **overrides) -> GaussianRegressionHead:
    torch.manual_seed(seed)
    return GaussianRegressionHead(attrs.evolve(HEAD_CONFIG, **overrides)).eval()


def _regressor(**overrides) -> GaussianImageRegressor:
    torch.manual_seed(23)
    encoder = ImageEncoder(ENCODER_CONFIG)
    head = GaussianRegressionHead(attrs.evolve(HEAD_CONFIG, **overrides))
    return GaussianImageRegressor(encoder, head).eval()


def _features(count: int = 4, *, seed: int = 29) -> torch.Tensor:
    generator = torch.Generator().manual_seed(seed)
    return torch.randn((count, HEAD_CONFIG.input_features), generator=generator)


def _head_trained_towards_zero(*, seed: int = 17) -> GaussianRegressionHead:
    """平均が全 sample で死んだ head を返す.

    平均を下げ続けると bias が負へ抜け、全 sample が同時に ReLU の死んだ領域へ入る。

    実装ノート R-A の「平均 head が死んだ run」と同じ状態。
    """

    head = _head(seed=seed, mean_bias_initial=SATURATION_PROBE_BIAS)
    optimizer = torch.optim.Adam(head.parameters(), lr=SATURATION_LEARNING_RATE)
    optimizer.zero_grad()
    head(_features(seed=SATURATION_TRAINING_SEED))[0].sum().backward()
    optimizer.step()
    return head


def _head_trained_to_partially_saturate(*, seed: int = 17) -> GaussianRegressionHead:
    """1 sample だけが死んだ head を返す.

    真値 0 の blank を 1 件だけ混ぜて回帰させる。

    その sample の前活性だけが負へ抜ける。ReLU 化が狙った状態そのもの。
    """

    head = _head(seed=seed, mean_bias_initial=SATURATION_PROBE_BIAS)
    optimizer = torch.optim.Adam(head.parameters(), lr=SATURATION_LEARNING_RATE)
    features = _features(seed=SATURATION_TRAINING_SEED)
    target = torch.full((features.shape[0], 1), 2.0)
    target[SATURATED_INDEX] = 0.0
    for _ in range(PARTIAL_SATURATION_STEPS):
        optimizer.zero_grad()
        mean, _ = head(features)
        ((mean - target) ** 2).mean().backward()
        optimizer.step()
    return head


# 既定の mean_bias_initial=1.0 では head が飽和しないので（それが狙い）、ReLU の
# 厳密 0 を観測するときだけ下駄をほぼ外した設定を使う。正の有限値なので validate は通る。
SATURATION_PROBE_BIAS = 1e-3

# 平均出力層の weight は 0 初期化なので、初期状態では前活性が bias 一定で負へ落ちない。
# ReLU の飽和は学習が始まってから起きるので、観測用の head は少し学習させて作る。
SATURATION_LEARNING_RATE = 0.05
SATURATION_TRAINING_SEED = 29
PARTIAL_SATURATION_STEPS = 200

# 平均出力層の weight を 0 から動かし、条件変数が平均へ届くのを観測するための step 数
CONDITIONING_TRAINING_STEPS = 20

# 飽和の観測に使う特徴量の種と、blank として真値 0 を当てる sample の位置
SATURATION_FEATURE_SEED = 15
SATURATED_INDEX = 2

# 平均 head が初期化時点で死んでいないことを確かめる seed 数。
#
# weight 0 初期化を外したとき、この config（32/16）と SMALL_MEAN_BIAS で
# batch の 1 つ以上が死ぬ確率は、全 0 feature 12% / 標準正規 feature 77%
# （batch 4、300 seed 実測）。
# 全 0 側は最初の死亡が seed 13 なので、**32 を 13 未満へ下げると検出器が空になる**。
# 標準正規側は seed 0 で落ちるが、そちらだけに頼ると片方の regime を見失う。
BIRTH_CHECK_SEEDS = 32

# ドメイン側が真値スケール（0.05〜0.2 µL）へ合わせて上書きする想定の小さな bias
SMALL_MEAN_BIAS = 0.05


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

    @pytest.mark.parametrize("mean_bias_initial", [0.0, -0.5, float("nan")])
    def test_rejects_a_mean_bias_that_is_not_a_positive_finite_value(
        self, mean_bias_initial: float
    ):
        """平均出力層の初期 bias は正の有限値に限る.

        非正だと初期化時点で batch 全体が負の前活性へ落ちる。

        ReLU の出力も勾配も 0 になり、平均 head が恒久的に死ぬ。

        この検査そのものが、その欠陥を防ぐ機構になっている。
        """

        config = attrs.evolve(HEAD_CONFIG, mean_bias_initial=mean_bias_initial)

        reason = config.validate()

        assert reason is not None
        assert "mean_bias_initial" in reason
        assert str(mean_bias_initial) in reason

    def test_the_default_mean_bias_is_positive(self):
        assert HEAD_CONFIG.mean_bias_initial > 0


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

    def test_mean_is_non_negative(self):
        mean, _ = _head()(_features())

        assert bool((mean >= 0).all())

    @pytest.mark.parametrize("mean_bias_initial", [1.0, SMALL_MEAN_BIAS])
    @pytest.mark.parametrize("uniform_features", [True, False])
    def test_the_mean_head_is_never_born_dead(
        self, mean_bias_initial: float, uniform_features: bool
    ):
        """どの初期化 seed でも batch のどの sample も 0 にはならない.

        平均出力層は weight を 0、bias を正から始めるので、初期の前活性は bias
        そのものになる。**どんな正の bias でも活性領域に入る。**

        bias だけを正にする対処では足りない。weight が torch 既定のままだと
        ``weight @ hidden`` の広がりが小さい bias を飲み込む。

        ドメイン側は真値スケールへ合わせて小さい値へ上書きする想定なので、既定の
        1.0 と小さい値の両方で守られていることを見る。

        **検出を担っているのは ``mean_bias_initial=SMALL_MEAN_BIAS`` の 2 ケースだけ**
        で、1.0 の 2 ケースは weight 0 初期化を外しても落ちない。

        全 0 の feature（trunk 出力が sample 間で同一）と標準正規の feature の両方で
        確かめる。weight 0 初期化を外したとき batch の 1 つ以上が死ぬ確率は前者 12%、
        後者 77%（この config、batch 4、300 seed 実測）。

        1 sample でも死ねばその sample の平均側の勾配が消えるので、batch 全滅
        （標準正規では 11%）ではなくこちらを見る。

        seed 数を減らすと検出力が消える。詳細は :data:`BIRTH_CHECK_SEEDS`。
        """

        features = (
            torch.zeros(4, HEAD_CONFIG.input_features)
            if uniform_features
            else _features()
        )

        for seed in range(BIRTH_CHECK_SEEDS):
            head = _head(seed=seed, mean_bias_initial=mean_bias_initial)

            mean, _ = head(features)

            assert bool((mean > 0).all()), f"seed {seed} で平均 head が死んでいます"

    def test_the_mean_head_still_learns_from_a_zero_weight(self):
        """Weight を 0 から始めても勾配が流れ、0 に固定されない.

        初期の前活性が ``bias > 0`` なので ReLU の微分は 1 になり、weight へ
        ``grad_z * hidden`` が伝わる。
        """

        head = _head(mean_bias_initial=SMALL_MEAN_BIAS)
        optimizer = torch.optim.Adam(head.parameters(), lr=SATURATION_LEARNING_RATE)
        features = _features()

        for _ in range(3):
            optimizer.zero_grad()
            mean, _ = head(features)
            ((mean - 2.0) ** 2).mean().backward()
            optimizer.step()

        moved, _ = head(features)
        assert bool((moved > 0).all())
        # 全 sample で同じ値なら weight が 0 のまま
        assert float(moved.max().item()) > float(moved.min().item())

    def test_mean_is_exactly_zero_when_the_pre_activation_is_negative(self):
        """平均活性化は ReLU なので、負の前活性は厳密な 0 になる.

        Softplus へ巻き戻すと、同じ入力での最小出力が 2.4e-2 程度になり厳密な 0
        を出せない。真値 0 の blank を表現できないので、ここは近似ではなく
        ``== 0.0`` の厳密比較で固定する。
        """

        head = _head_trained_towards_zero()

        mean, _ = head(_features(seed=SATURATION_FEATURE_SEED))

        assert torch.equal(mean, torch.zeros_like(mean))

    def test_a_saturated_sample_has_exactly_zero_gradient_to_its_features(self):
        """飽和した sample は平均側の勾配が厳密に 0 になる.

        「出力が厳密に 0」と「前活性への勾配が 0」は ReLU では同値（実測 64/64）。

        Softplus は出力が 0 にならないので勾配も 0 にならない。

        飽和していない sample の勾配が非零であることも同時に見る。

        勾配を一律で潰す変異と区別するため。
        """

        features = _features(seed=SATURATION_TRAINING_SEED)
        features.requires_grad_(True)

        head = _head_trained_to_partially_saturate()

        mean, _ = head(features)
        mean.sum().backward()

        assert features.grad is not None
        assert float(mean[SATURATED_INDEX].item()) == 0.0
        assert torch.equal(
            features.grad[SATURATED_INDEX],
            torch.zeros_like(features.grad[SATURATED_INDEX]),
        )
        for index in range(features.shape[0]):
            if index == SATURATED_INDEX:
                continue
            assert float(mean[index].item()) > 0.0
            assert bool((features.grad[index] != 0).any())

    def test_mean_stays_non_negative_and_finite_for_extreme_features(self):
        # 極端な入力でも ReLU は非有限値を作らない。相対誤差を出す評価側は
        # mean <= 0 の sample を無効として除外する。
        mean, _ = _head()(_features() * 1e6)

        assert bool((mean >= 0).all())
        assert bool(torch.isfinite(mean).all())

    def test_log_variance_stays_inside_the_configured_range(self):
        head = _head(log_variance_minimum=-3.0, log_variance_maximum=2.0)

        _, log_variance = head(_features())

        assert bool((log_variance >= -3.0).all())
        assert bool((log_variance <= 2.0).all())

    def test_log_variance_clamps_exactly_onto_the_configured_bounds(self):
        """極端な入力では log 分散が上下限そのものへ張り付く.

        平均側を ReLU へ変えた巻き添えで clamp が緩む変異を、境界値との厳密一致で捕まえる。

        範囲内に収まっているだけの assert では検出できない。
        """

        head = _head(log_variance_minimum=-3.0, log_variance_maximum=2.0)

        _, log_variance = head(_features() * 1e6)

        assert bool(((log_variance == -3.0) | (log_variance == 2.0)).all())

    def test_conditioning_changes_the_prediction(self):
        """条件変数が共有 trunk を通って**両方の**出力へ届く.

        効かせたい先は平均。``log(pixel_per_mm)`` を連結する狙いが物理 scale を
        平均へ反映させることなので、log 分散側だけでは契約を固定できない。

        平均出力層は weight を 0 から始めるので、初期化直後の平均は入力に依らず
        bias 一定になる。平均側は数 step 学習させて weight を動かしてから観測する。
        """

        head = _head(conditioning_features=2)
        features = _features()
        conditioning = torch.zeros(4, 2)

        _, baseline_log_variance = head(features, conditioning)
        _, shifted_log_variance = head(features, conditioning + 1.0)

        assert not bool(torch.allclose(baseline_log_variance, shifted_log_variance))

        optimizer = torch.optim.Adam(head.parameters(), lr=SATURATION_LEARNING_RATE)
        for _ in range(CONDITIONING_TRAINING_STEPS):
            optimizer.zero_grad()
            mean, _ = head(features, conditioning)
            ((mean - 2.0) ** 2).mean().backward()
            optimizer.step()

        baseline_mean, _ = head(features, conditioning)
        shifted_mean, _ = head(features, conditioning + 1.0)

        assert not bool(torch.allclose(baseline_mean, shifted_mean))

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


class TestExportedDynamicShapes:
    """``torch.export`` 後も batch 次元が固定されない.

    条件変数と feature の batch 一致検査に ``int()`` が入ると、非 strict export が
    SymInt を example の batch へ落とし、``dynamic_shapes`` の宣言が黙って
    無視される。

    ``strict=False`` を明示するのは、torch 側の既定が変わってもこの検出力を
    保つため。strict 経路（dynamo）では ``int()`` があっても特殊化されない。
    """

    def test_accepts_another_batch_size_with_conditioning(self):
        head = _head(conditioning_features=2)
        batch = {0: torch.export.Dim.AUTO}

        exported = torch.export.export(
            head,
            (torch.randn(2, HEAD_CONFIG.input_features), torch.zeros(2, 2)),
            dynamic_shapes={"features": batch, "conditioning": batch},
            strict=False,
        )
        mean, log_variance = exported.module()(
            torch.randn(5, HEAD_CONFIG.input_features), torch.zeros(5, 2)
        )

        assert tuple(mean.shape) == (5, 1)
        assert tuple(log_variance.shape) == (5, 1)

    def test_accepts_another_batch_size_through_the_regressor(self):
        regressor = _regressor(conditioning_features=2)
        batch = {0: torch.export.Dim.AUTO}

        exported = torch.export.export(
            regressor,
            (torch.randn(2, 3, 32, 32), None, torch.zeros(2, 2)),
            dynamic_shapes={
                "images": batch,
                "valid_pixel_mask": None,
                "conditioning": batch,
            },
            strict=False,
        )
        mean, _ = exported.module()(torch.randn(5, 3, 32, 32), None, torch.zeros(5, 2))

        assert tuple(mean.shape) == (5, 1)
