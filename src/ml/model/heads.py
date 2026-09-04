"""正の量を Gaussian で回帰する head と、encoder との合成 model.

出力は平均と log 分散の 2 本とする。

``ml.model.loss`` の negative log likelihood と対になる形。

平均は Softplus で常に非負にする。

対象を正の物理量の回帰に限っているため。

評価側も相対誤差と coverage で正の target を前提にしている。

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
        self._mean = nn.Linear(config.hidden_features, 1)
        self._log_variance = nn.Linear(config.hidden_features, 1)
        self._mean_activation = nn.Softplus()

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
        if int(conditioning.shape[0]) != int(features.shape[0]):
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
