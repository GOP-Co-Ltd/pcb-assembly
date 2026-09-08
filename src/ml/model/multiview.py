"""同一対象を複数視点から撮った sample を 1 個の feature へ落とす model.

view 軸を持つ ``[B, V, C, H, W]`` を、共有 encoder で符号化してから順序不変に
集約する。

view が何であるか（撮像位置、offset、番号の割り当て）はドメイン側が持つ。

集約は平均とする。

parameter を持たないので view 数が変わっても退化せず、V=5 で学習した graph を
V=1 で実行できる。

5 view は中心と対称な 4 方向で、view 間に先験的な優劣が無いことも理由。
"""

from __future__ import annotations

from typing import override

from torch import Tensor, nn

from ml.model.blocks import ImageEncoder
from ml.model.heads import GaussianRegressionHead


class MultiViewImageEncoder(nn.Module):
    """View 軸を平坦化して共有 encoder へ通し、平均で集約する encoder.

    view ごとに別の重みを持たせない。

    view は同じ対象を少しずらして撮ったものなので、符号化の規則を分ける根拠が無い。
    """

    def __init__(self, encoder: ImageEncoder) -> None:
        super().__init__()
        self._encoder = encoder

    @property
    def encoder(self) -> ImageEncoder:
        """全 view が共有する画像 encoder."""

        return self._encoder

    @property
    def output_features(self) -> int:
        """Forward が返す feature 次元数."""

        return self._encoder.output_features

    @override
    def forward(self, images: Tensor, valid_pixel_mask: Tensor | None = None) -> Tensor:
        """``[B, V, C, H, W]`` を ``[B, output_features]`` へ変換する."""

        _reject_invalid_inputs(images, valid_pixel_mask)
        # 各軸へ int() を掛けると、非 strict export（torch.export の既定）が
        # SymInt を example 入力の値へ落とし、dynamic_shapes の宣言が黙って
        # 無視される。batch と view の大きさは SymInt のまま扱う。
        batch_size = images.shape[0]
        view_count = images.shape[1]
        flattened_mask = (
            None if valid_pixel_mask is None else valid_pixel_mask.flatten(0, 1)
        )
        features: Tensor = self._encoder(images.flatten(0, 1), flattened_mask)
        return features.unflatten(0, (batch_size, view_count)).mean(dim=1)


class MultiViewGaussianRegressor(nn.Module):
    """多視点 encoder と Gaussian head をつないだ単一 model.

    ONNX export と ``torch.compile`` の対象を 1 個の module に保つため、合成を
    ``ml`` 側で提供する。
    """

    def __init__(
        self, encoder: MultiViewImageEncoder, head: GaussianRegressionHead
    ) -> None:
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
        """多視点画像から平均 ``[B, 1]`` と log 分散 ``[B, 1]`` を返す."""

        features: Tensor = self._encoder(images, valid_pixel_mask)
        mean, log_variance = self._head(features, conditioning)
        return mean, log_variance


def _reject_invalid_inputs(images: Tensor, valid_pixel_mask: Tensor | None) -> None:
    """View 軸の有無と、batch / view 軸の一致だけを見る.

    channel 数・mask の dtype・mask の空間 shape は ``flatten(0, 1)`` のあと
    :class:`~ml.model.blocks.ImageEncoder` が捕まえるので、ここでは重ねない。

    同じ不整合に検出器を 2 つ置くと、前段が後段を隠して回帰に気付けなくなる。

    batch と view の一致だけは例外で、平坦化すると ``B * V`` しか残らず
    ``ImageEncoder`` からは観測できない。

    ``[2, 3, ...]`` の images へ ``[3, 2, 1, ...]`` の mask を渡すと、どちらも 6 に
    なって検査を通り、違う view の mask を当てたまま結果が返る。
    """

    if images.ndim != 5:
        raise ValueError(f"images は [B, V, C, H, W] が必要です: {tuple(images.shape)}")
    if valid_pixel_mask is None:
        return
    if valid_pixel_mask.ndim != 5:
        raise ValueError(
            "valid pixel mask は [B, V, 1, H, W] が必要です: "
            f"{tuple(valid_pixel_mask.shape)}"
        )
    # 各軸へ int() を掛けると、非 strict export（torch.export の既定）が SymInt を
    # example 入力の値へ落とし、dynamic_shapes の宣言が黙って無視される。
    # SymInt のまま比較する。
    if valid_pixel_mask.shape[:2] != images.shape[:2]:
        raise ValueError(
            "valid pixel mask の batch と view が images と一致しません: "
            f"{tuple(valid_pixel_mask.shape[:2])} と {tuple(images.shape[:2])}"
        )


__all__ = [
    "MultiViewGaussianRegressor",
    "MultiViewImageEncoder",
]
