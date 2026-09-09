"""塗布量推定 v1 encoder の組み立てと、その fine-tune 範囲.

新しい ``nn.Module`` は 1 つも定義しない。

仕様書 §2 の v1 諸元は :class:`~ml.model.blocks.ImageEncoderConfig` と
:class:`~ml.model.heads.GaussianHeadConfig` へそのまま写せるので、ドメイン側が持つのは
既定値・実測による検証・freeze 範囲だけとする。

``mean_bias_initial`` だけは ``ml`` の中立な既定 1.0 を使えない。

平均 head の ReLU は前活性が負へ落ちた sample の勾配を切るので、真値のスケール
（実データ 816 sample の measured 平均 0.1652 / median 0.1601）に近い正の値から始める。

この値を train split の統計から決めない。

fold ごとに model config が変わると run 間の比較が読めなくなる。
"""

from __future__ import annotations

from pathlib import Path

import attrs
import torch

from ml.data.image import ImageConstraints
from ml.model.blocks import ImageEncoder, ImageEncoderConfig
from ml.model.heads import GaussianHeadConfig, GaussianRegressionHead
from ml.model.inspection import ModelSize
from ml.model.multiview import MultiViewGaussianRegressor, MultiViewImageEncoder

MODEL_FAMILY = "paste-volume-resnet-small-v1"

# pre RGB 3 + post RGB 3 を channel 方向へ連結した入力
INPUT_CHANNELS = 6

# 条件変数は log(有効 pixel_per_mm) の 1 本だけ
CONDITIONING_FEATURES = 1

# ``MultiViewGaussianRegressor`` の state_dict キーのうち、freeze 範囲を決めるのに使う接頭辞。
#
# state_dict のキーは checkpoint と export の公開契約なので、ここで名前に依存してよい。
# それでも取りこぼしが黙って通らないよう、:func:`apply_fine_tune_freeze` が実在を確かめる。
_STEM_PREFIX = "_encoder._encoder._stem."
_STAGES_PREFIX = "_encoder._encoder._stages."
_PADDING_PIXEL_NAME = "_encoder._encoder._padding_pixel"


@attrs.frozen
class PasteVolumeModelConfig:
    """V1 encoder の形.

    既定値が仕様書 §2 の確定値そのもの。
    """

    stem_channels: tuple[int, ...] = (24, 32)
    stem_strides: tuple[int, ...] = (2, 2)
    stage_channels: tuple[int, ...] = (48, 96)
    stage_strides: tuple[int, ...] = (1, 2)
    blocks_per_stage: tuple[int, ...] = (2, 2)
    group_norm_groups: int = 8
    hidden_features: int = 128
    log_variance_minimum: float = -14.0
    log_variance_maximum: float = 5.0

    mean_bias_initial: float = 0.15
    """平均線形層の bias 初期値。真値スケール 0.05〜0.2 uL の内側に置く."""

    initial_weights: Path | None = None
    """Fine-tune の起点にする weight。読み込みは entrypoint 側の責務."""

    fine_tune: bool = False
    """更新範囲を最終 stage 以降へ絞るか。凍結は entrypoint 側の責務."""

    def validate(self) -> str | None:
        """Encoder と head の設定をまとめて検証する.

        encoder を先に見る。

        head の ``input_features`` は encoder の最終 stage channel から決まるので、
        stage が空の設定では head 側を組み立てられない。
        """

        if error := self.encoder_config().validate():
            return error
        return self.head_config().validate()

    def encoder_config(self) -> ImageEncoderConfig:
        """畳み込み encoder 側の設定へ写す."""

        return ImageEncoderConfig(
            input_channels=INPUT_CHANNELS,
            stem_channels=self.stem_channels,
            stem_strides=self.stem_strides,
            stage_channels=self.stage_channels,
            stage_strides=self.stage_strides,
            blocks_per_stage=self.blocks_per_stage,
            group_norm_groups=self.group_norm_groups,
        )

    def head_config(self) -> GaussianHeadConfig:
        """Gaussian 回帰 head 側の設定へ写す."""

        return GaussianHeadConfig(
            input_features=self.encoder_config().output_features,
            conditioning_features=CONDITIONING_FEATURES,
            hidden_features=self.hidden_features,
            log_variance_minimum=self.log_variance_minimum,
            log_variance_maximum=self.log_variance_maximum,
            mean_bias_initial=self.mean_bias_initial,
        )

    def validate_for_constraints(self, constraints: ImageConstraints) -> str | None:
        """前処理が出す最小入力に対して、縮小率が過剰でないかも含めて検証する."""

        if error := self.validate():
            return error
        return self.encoder_config().validate_for_constraints(constraints)


def build_paste_volume_model(
    config: PasteVolumeModelConfig,
) -> tuple[MultiViewGaussianRegressor | None, str | None]:
    """設定から多視点 Gaussian 回帰 model を組む.

    ``initial_weights`` の読み込みと ``fine_tune`` の freeze は行わない。

    どちらも「組んだあとに何をするか」で、entrypoint が順番を持つ。
    """

    if error := config.validate():
        return None, error
    encoder = MultiViewImageEncoder(ImageEncoder(config.encoder_config()))
    head = GaussianRegressionHead(config.head_config())
    return MultiViewGaussianRegressor(encoder, head), None


def apply_fine_tune_freeze(model: MultiViewGaussianRegressor) -> tuple[str, ...]:
    """Stem と最終 stage 以外の residual stage を凍結し、凍結した parameter 名を返す.

    更新対象に残すのは最終 residual stage、pooling 後の Linear、2 個の head、
    learnable padding pixel。

    仕様書 §2 は更新対象を「residual stage 3」と書くが、v1 の encoder は stage を
    2 つしか持たない。計画書はこれを「最終 stage」と読み替えており、ここもその
    読み替えに従う。

    仕様書 §2 は learnable padding pixel を更新対象に挙げるだけで理由を書いていない。
    fine-tune 先の Raspberry Pi では撮像条件が変わり padding 領域の扱いを学び直す
    必要がある、というのがここでの解釈。

    期待する接頭辞が 1 つでも見つからなければ例外にする。

    ``ml`` 側の属性名が変わったときに「1 つも凍結しない freeze」が黙って成功するのを
    防ぐため。
    """

    names = [name for name, _ in model.named_parameters()]
    stage_indices = sorted(
        {_stage_index(name) for name in names if name.startswith(_STAGES_PREFIX)}
    )
    if not stage_indices:
        raise ValueError(
            f"residual stage の parameter が見つかりません: {_STAGES_PREFIX}"
        )
    if not any(name.startswith(_STEM_PREFIX) for name in names):
        raise ValueError(f"stem の parameter が見つかりません: {_STEM_PREFIX}")
    if _PADDING_PIXEL_NAME not in names:
        raise ValueError(
            f"learnable padding pixel が見つかりません: {_PADDING_PIXEL_NAME}"
        )
    last_stage = stage_indices[-1]
    frozen: list[str] = []
    for name, parameter in model.named_parameters():
        if not _is_frozen_by_fine_tune(name, last_stage=last_stage):
            continue
        parameter.requires_grad_(False)
        frozen.append(name)
    return tuple(frozen)


def measure_paste_volume_model(
    model: MultiViewGaussianRegressor, *, height: int, width: int, view_count: int
) -> ModelSize:
    """1 sample を推論するときの parameter 数と multiply-accumulate 数を実測する.

    入力は model が置かれている device 上に作る。
    """

    device = next(model.parameters()).device
    images = torch.zeros(
        (1, view_count, INPUT_CHANNELS, height, width),
        dtype=torch.float32,
        device=device,
    )
    valid_pixel_mask = torch.ones(
        (1, view_count, 1, height, width), dtype=torch.bool, device=device
    )
    conditioning = torch.zeros(
        (1, CONDITIONING_FEATURES), dtype=torch.float32, device=device
    )
    return ModelSize.measure(model, (images, valid_pixel_mask, conditioning))


def _is_frozen_by_fine_tune(name: str, *, last_stage: int) -> bool:
    if name.startswith(_STEM_PREFIX):
        return True
    return name.startswith(_STAGES_PREFIX) and _stage_index(name) != last_stage


def _stage_index(name: str) -> int:
    """``_stages.<i>....`` の stage 番号を取り出す."""

    return int(name.removeprefix(_STAGES_PREFIX).split(".", maxsplit=1)[0])


__all__ = [
    "CONDITIONING_FEATURES",
    "INPUT_CHANNELS",
    "MODEL_FAMILY",
    "PasteVolumeModelConfig",
    "apply_fine_tune_freeze",
    "build_paste_volume_model",
    "measure_paste_volume_model",
]
