"""学習 task の契約と、Gaussian 回帰の具象 task.

Trainer は batch の中身も loss の形も知らない。

知っているのは「batch を渡すと微分可能な 0 次元 loss と観測値が返る」ことだけで、
その境界がこの module の :class:`TrainingTask` になる。

compile は task の内側に閉じる。

Trainer が batch 型を知らずに ``torch.compile`` を有効化でき、
:attr:`TrainingTask.model` は常に compile 前の module を返せる。
"""

from __future__ import annotations

import abc
from collections.abc import Callable, Mapping, Sequence
from typing import cast, override

import attrs
import torch
from torch import Tensor, nn

from ml.evaluation.compile_parity import CompileOptions
from ml.evaluation.regression import (
    GaussianPredictions,
    GaussianRegressionMetrics,
    MeanSaturationDiagnostic,
)
from ml.model.heads import GaussianImageRegressor
from ml.model.loss import weighted_gaussian_negative_log_likelihood


@attrs.frozen(eq=False)
class StepResult[ObservationT]:
    """1 batch 分の loss と、commit 後に集計へ渡す観測値.

    Tensor は要素ごとの比較になり真偽値へ落ちないので、等価性は identity で決める。
    """

    loss: Tensor
    observation: ObservationT
    sample_count: int

    def validate(self) -> str | None:
        """Loss の形と sample 件数を検証する."""

        if self.loss.ndim != 0:
            return f"loss は 0 次元 tensor が必要です: {tuple(self.loss.shape)}"
        if self.sample_count < 1:
            return f"sample_count は正の整数が必要です: {self.sample_count}"
        return None


class TrainingTask[BatchT, ObservationT](abc.ABC):
    """Batch を loss と観測値へ変換する境界."""

    @property
    @abc.abstractmethod
    def model(self) -> nn.Module:
        """状態を持つ本体の module.

        ``compile_forward`` のあとも、常に compile 前の module を返すこと。

        ``state_dict`` / ``parameters`` / ``train`` / ``eval`` はすべてこの
        module に対して行われる。

        この module の ``state_dict()`` のキーは checkpoint と export の
        公開契約であり、内部属性のリネームは互換性を壊す。
        """

    @abc.abstractmethod
    def training_step(self, batch: BatchT) -> StepResult[ObservationT]:
        """微分可能な loss と、commit 後に使う観測値を返す."""

    @abc.abstractmethod
    def evaluation_step(self, batch: BatchT) -> ObservationT:
        """勾配を作らずに観測値だけを返す."""

    @abc.abstractmethod
    def reduce(self, observations: Sequence[ObservationT]) -> Mapping[str, float]:
        """観測値を metric 名から値への写像へ集計する.

        集計できないときは例外を投げず、空の写像を返すこと。
        """

    def compile_forward(self, options: CompileOptions) -> None:
        """Forward 経路を ``torch.compile`` 済みのものへ差し替える.

        既定は何もしない。

        compile を使う task だけが上書きする。
        """


@attrs.frozen(eq=False)
class GaussianBatch:
    """Gaussian 回帰 task が受け取る 1 batch.

    Tensor は要素ごとの比較になり真偽値へ落ちないので、等価性は identity で決める。
    """

    images: Tensor
    valid_pixel_mask: Tensor | None
    conditioning: Tensor | None
    target: Tensor
    sample_weight: Tensor

    def validate(self) -> str | None:
        """Batch 内の shape と dtype の整合を検証する."""

        if self.images.ndim != 4:
            return f"images は [B, C, H, W] が必要です: {tuple(self.images.shape)}"
        batch_size = int(self.images.shape[0])
        for name in ("target", "sample_weight"):
            values: Tensor = getattr(self, name)
            if tuple(values.shape) != (batch_size, 1):
                return (
                    f"{name} は [B, 1] が必要です: {tuple(values.shape)}"
                    f"（batch size {batch_size}）"
                )
        if self.valid_pixel_mask is not None:
            expected = (batch_size, 1, *tuple(self.images.shape[2:]))
            if tuple(self.valid_pixel_mask.shape) != expected:
                return (
                    "valid_pixel_mask は [B, 1, H, W] が必要です: "
                    f"{tuple(self.valid_pixel_mask.shape)}（期待値 {expected}）"
                )
            if self.valid_pixel_mask.dtype != torch.bool:
                return (
                    "valid_pixel_mask は bool が必要です: "
                    f"{self.valid_pixel_mask.dtype}"
                )
        if self.conditioning is not None:
            if self.conditioning.ndim != 2:
                return (
                    "conditioning は [B, K] が必要です: "
                    f"{tuple(self.conditioning.shape)}"
                )
            if int(self.conditioning.shape[0]) != batch_size:
                return (
                    "conditioning の batch size が images と一致しません: "
                    f"{int(self.conditioning.shape[0])} と {batch_size}"
                )
        return None


@attrs.frozen(eq=False)
class GaussianObservation:
    """1 batch 分の予測と実測.

    Tensor は要素ごとの比較になり真偽値へ落ちないので、等価性は identity で決める。
    """

    mean: Tensor
    log_variance: Tensor
    target: Tensor
    sample_weight: Tensor


class GaussianRegressionTask(TrainingTask[GaussianBatch, GaussianObservation]):
    """:class:`GaussianImageRegressor` を重み付き negative log likelihood で学習する.

    compile 済み callable は別の属性に持ち、:attr:`model` は常に元の module を返す。

    こうしないと ``state_dict()`` のキーへ ``_orig_mod.`` が混ざる。
    """

    def __init__(self, model: GaussianImageRegressor) -> None:
        self._model = model
        self._forward: Callable[..., tuple[Tensor, Tensor]] = model

    @property
    @override
    def model(self) -> nn.Module:
        """Compile 前の本体 module."""

        return self._model

    @override
    def training_step(self, batch: GaussianBatch) -> StepResult[GaussianObservation]:
        """微分可能な negative log likelihood と、detach 済み観測値を返す."""

        if error := batch.validate():
            raise ValueError(error)
        mean, log_variance = self._forward(
            batch.images, batch.valid_pixel_mask, batch.conditioning
        )
        loss = weighted_gaussian_negative_log_likelihood(
            mean, log_variance, batch.target, batch.sample_weight
        )
        return StepResult(
            loss=loss,
            observation=GaussianObservation(
                mean=mean.detach(),
                log_variance=log_variance.detach(),
                target=batch.target.detach(),
                sample_weight=batch.sample_weight.detach(),
            ),
            sample_count=int(batch.images.shape[0]),
        )

    @override
    def evaluation_step(self, batch: GaussianBatch) -> GaussianObservation:
        """勾配を作らずに予測と実測を返す."""

        if error := batch.validate():
            raise ValueError(error)
        with torch.no_grad():
            mean, log_variance = self._forward(
                batch.images, batch.valid_pixel_mask, batch.conditioning
            )
        return GaussianObservation(
            mean=mean.detach(),
            log_variance=log_variance.detach(),
            target=batch.target.detach(),
            sample_weight=batch.sample_weight.detach(),
        )

    @override
    def reduce(
        self, observations: Sequence[GaussianObservation]
    ) -> Mapping[str, float]:
        """観測値を連結し、回帰 metric と平均飽和の診断を返す.

        平均 head の ReLU が全 sample で 0 に張り付くと、``valid_sample_mask`` が
        ``mean > 0`` を要求するため回帰 metric を 1 つも出せない。

        そこで :class:`MeanSaturationDiagnostic` の各項は metric の可否に関わらず
        必ず返す。

        空の写像を返すと運用者が受け取るのは Trainer の「monitor がありません」
        だけになり、真の原因である飽和が読み取れなくなるため。

        主要 monitor を欠かせること自体は変えない。評価できない run は
        Trainer が従来どおり止める。
        """

        if not observations:
            return {}
        predictions = GaussianPredictions(
            mean=torch.cat([item.mean for item in observations]),
            log_variance=torch.cat([item.log_variance for item in observations]),
            target=torch.cat([item.target for item in observations]),
            sample_weight=torch.cat([item.sample_weight for item in observations]),
        )
        if predictions.validate():
            return {}
        values = _as_float_mapping(MeanSaturationDiagnostic.measure(predictions))
        metrics, _ = GaussianRegressionMetrics.measure(predictions)
        if metrics is not None:
            values.update(_as_float_mapping(metrics))
        return values

    @override
    def compile_forward(self, options: CompileOptions) -> None:
        """Forward だけを ``torch.compile`` 済み callable へ差し替える."""

        if error := options.validate():
            raise ValueError(error)
        self._forward = cast(
            Callable[..., tuple[Tensor, Tensor]],
            torch.compile(
                self._model,
                backend=options.backend,
                mode=options.mode,
                fullgraph=options.fullgraph,
                dynamic=options.dynamic,
            ),
        )


def _as_float_mapping(record: attrs.AttrsInstance) -> dict[str, float]:
    """Frozen な集計結果を、logger へ渡せる float の写像へ落とす."""

    return {
        name: float(cast(float, value)) for name, value in attrs.asdict(record).items()
    }


__all__ = [
    "GaussianBatch",
    "GaussianObservation",
    "GaussianRegressionTask",
    "StepResult",
    "TrainingTask",
]
