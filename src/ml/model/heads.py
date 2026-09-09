"""正の量を Gaussian で回帰する head と、encoder との合成 model.

出力は平均と log 分散の 2 本とする。

``ml.model.loss`` の negative log likelihood と対になる形。

平均は ReLU で非負にする。

真値 0 の blank sample を厳密な 0 として表現できるようにするため。

Softplus は厳密な 0 を出せず、blank へ必ず正の下駄を履かせてしまう。

代償として、前活性が負へ落ちた sample は平均側の勾配が 0 になる。

平均線形層は weight を 0、bias を ``mean_bias_initial`` の正値から始める。

初期の前活性が bias そのものになるので、どんな正の値でも活性領域に入る。

学習開始時に全 sample が死んだ領域へ入るのを防ぐための組み合わせで、片方だけでは効かない。

望ましい初期平均は真値のスケール次第なので、値そのものはドメイン側が設定する。

学習途中で死んだ領域へ落ちる sample は
:class:`~ml.evaluation.regression.MeanSaturationDiagnostic` で監視する。

log 分散は設定した範囲へ clamp する。

学習初期に分散が発散・消失して勾配が壊れるのを防ぐための措置。

範囲外で勾配が切れるのは意図どおり。
"""

from __future__ import annotations

import math
from typing import override

import attrs
import torch
from torch import Tensor, nn

from ml.model.blocks import ImageEncoder


@attrs.frozen
class GaussianHeadConfig:
    """Gaussian 回帰 head の形を決める設定."""

    input_features: int
    conditioning_features: int = 0
    hidden_features: int = 128
    log_variance_minimum: float = -14.0
    log_variance_maximum: float = 5.0
    mean_bias_initial: float = 1.0

    def validate(self) -> str | None:
        """Head 設定の整合を検証する."""

        if self.input_features < 1:
            return f"input_features は正の整数が必要です: {self.input_features}"
        if self.conditioning_features < 0:
            return (
                "conditioning_features は 0 以上が必要です: "
                f"{self.conditioning_features}"
            )
        if self.hidden_features < 1:
            return f"hidden_features は正の整数が必要です: {self.hidden_features}"
        if not math.isfinite(self.mean_bias_initial) or self.mean_bias_initial <= 0:
            return f"mean_bias_initial は正の有限値が必要です: {self.mean_bias_initial}"
        for name in ("log_variance_minimum", "log_variance_maximum"):
            value: float = getattr(self, name)
            if not math.isfinite(value):
                return f"{name} は有限値が必要です: {value}"
        if self.log_variance_minimum >= self.log_variance_maximum:
            return (
                "log_variance_maximum は log_variance_minimum より大きい値が"
                f"必要です: {self.log_variance_minimum} >= "
                f"{self.log_variance_maximum}"
            )
        return None


class GaussianRegressionHead(nn.Module):
    """Pooled feature と条件変数から平均と log 分散を出す head.

    平均と log 分散は共有 hidden 層のあと、独立した線形層で出す。
    """

    def __init__(self, config: GaussianHeadConfig) -> None:
        super().__init__()
        if error := config.validate():
            raise ValueError(error)
        self._config = config
        self._trunk = nn.Sequential(
            nn.Linear(
                config.input_features + config.conditioning_features,
                config.hidden_features,
            ),
            nn.ReLU(),
        )
        mean_layer = nn.Linear(config.hidden_features, 1)
        # bias の既定初期値は ±1/sqrt(hidden_features) の一様分布で、およそ半数の
        # 初期化が負になる。全 sample が同じ負の前活性へ落ちると ReLU が勾配を
        # 遮断し、平均 head が学習開始時から恒久的に死ぬ。正の値から始める。
        #
        # weight も 0 から始める。初期の前活性が bias そのものになるので、
        # mean_bias_initial がどんな正の値でも活性領域に入ることを保証できる。
        # bias だけを正にしても weight @ hidden の広がりが bias を上回れば死ぬ。
        # 前活性が正なら ReLU の微分は 1 なので weight へ勾配が流れ、0 に固定
        # されない。log_variance 側は ReLU を通らないので触らない。
        nn.init.constant_(mean_layer.bias, config.mean_bias_initial)
        nn.init.zeros_(mean_layer.weight)
        self._mean = mean_layer
        self._log_variance = nn.Linear(config.hidden_features, 1)
        self._mean_activation = nn.ReLU()

    @property
    def input_features(self) -> int:
        """Forward が受け取る feature 次元数."""

        return self._config.input_features

    @property
    def conditioning_features(self) -> int:
        """Forward が受け取る条件変数の次元数."""

        return self._config.conditioning_features

    @override
    def forward(
        self, features: Tensor, conditioning: Tensor | None = None
    ) -> tuple[Tensor, Tensor]:
        """``[B, F]``（と ``[B, K]``）から ``([B, 1], [B, 1])`` を返す."""

        hidden = self._trunk(self._joined_inputs(features, conditioning))
        mean: Tensor = self._mean_activation(self._mean(hidden))
        log_variance: Tensor = torch.clamp(
            self._log_variance(hidden),
            self._config.log_variance_minimum,
            self._config.log_variance_maximum,
        )
        return mean, log_variance

    def _joined_inputs(self, features: Tensor, conditioning: Tensor | None) -> Tensor:
        if features.ndim != 2:
            raise ValueError(f"features は [B, F] が必要です: {tuple(features.shape)}")
        if int(features.shape[1]) != self._config.input_features:
            raise ValueError(
                "features の次元数が config と一致しません: "
                f"{int(features.shape[1])}（期待値 {self._config.input_features}）"
            )
        if conditioning is None:
            if self._config.conditioning_features != 0:
                raise ValueError(
                    "conditioning が必要です: "
                    f"conditioning_features={self._config.conditioning_features}"
                )
            return features
        if conditioning.ndim != 2:
            raise ValueError(
                f"conditioning は [B, K] が必要です: {tuple(conditioning.shape)}"
            )
        if int(conditioning.shape[1]) != self._config.conditioning_features:
            raise ValueError(
                "conditioning の次元数が config と一致しません: "
                f"{int(conditioning.shape[1])}"
                f"（期待値 {self._config.conditioning_features}）"
            )
        # int() を掛けると、非 strict export（torch.export の既定）が batch を
        # example の値へ落とし、dynamic_shapes の宣言が黙って無視される。
        if conditioning.shape[0] != features.shape[0]:
            raise ValueError(
                "features と conditioning の batch size が一致しません: "
                f"{int(features.shape[0])} と {int(conditioning.shape[0])}"
            )
        if conditioning.dtype != features.dtype:
            raise ValueError(
                "features と conditioning の dtype が一致しません: "
                f"{features.dtype} と {conditioning.dtype}"
            )
        return torch.cat((features, conditioning), dim=1)


class GaussianImageRegressor(nn.Module):
    """画像 encoder と Gaussian head をつないだ単一 model.

    encoder と head の合成を ``ml`` 側で提供する。

    ONNX export と ``torch.compile`` の対象を 1 個の module に保つため。
    """

    def __init__(self, encoder: ImageEncoder, head: GaussianRegressionHead) -> None:
        super().__init__()
        if head.input_features != encoder.output_features:
            raise ValueError(
                "head の input_features が encoder の output_features と"
                f"一致しません: {head.input_features} と {encoder.output_features}"
            )
        self._encoder = encoder
        self._head = head

    @override
    def forward(
        self,
        images: Tensor,
        valid_pixel_mask: Tensor | None = None,
        conditioning: Tensor | None = None,
    ) -> tuple[Tensor, Tensor]:
        """画像から平均 ``[B, 1]`` と log 分散 ``[B, 1]`` を返す."""

        features: Tensor = self._encoder(images, valid_pixel_mask)
        mean, log_variance = self._head(features, conditioning)
        return mean, log_variance


__all__ = [
    "GaussianHeadConfig",
    "GaussianImageRegressor",
    "GaussianRegressionHead",
]
