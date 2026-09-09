"""``ml.training`` の契約の塗布量推定むけ実装.

Trainer は dataset の中身も loss の形も知らない。知っているのは「epoch ごとに sample ID の
並びが決まり、それを batch へ実体化できる」ことと「batch を渡すと微分可能な 0 次元 loss と
観測値が返る」ことだけ。ここはその境界を繋ぐだけの薄い層で、読み出しは
:mod:`ml.paste_volume.dataset`、前処理と詰め込みは :mod:`ml.paste_volume.batch`、model は
:mod:`ml.paste_volume.model` が担う。

``plan_epoch`` は純関数でなければならない。Trainer は checkpoint に載せた batch plan と
の厳密一致を要求するので、ここが epoch 途中の resume を支えている。

:class:`PasteVolumeTask` は :class:`~ml.training.task.GaussianRegressionTask` を継承も委譲も
しない。あちらは 4 次元 ``images`` の :class:`~ml.training.task.GaussianBatch` を前提にして
いて、view 軸を持つ :class:`~ml.paste_volume.batch.PasteVolumeBatch` を渡せない。loss の式は
:func:`~ml.model.loss.weighted_gaussian_negative_log_likelihood` の 1 本を共有するので、
複製されるのは ``reduce`` の集計だけとする。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Self, cast, override

import attrs
import torch
from torch import Tensor, nn

from ml.data.batch import BatchShape, plan_pixel_budget_batches
from ml.data.split import (
    LeaveOneGroupOutPlan,
    SplitManifest,
    SplitName,
    SplitRatios,
)
from ml.evaluation.compile_parity import CompileOptions
from ml.evaluation.regression import (
    GaussianPredictions,
    GaussianRegressionMetrics,
    MeanSaturationDiagnostic,
    ZeroTargetMetrics,
)
from ml.model.loss import weighted_gaussian_negative_log_likelihood
from ml.model.multiview import MultiViewGaussianRegressor
from ml.paste_volume.batch import PasteVolumeBatch, PasteVolumeCollator
from ml.paste_volume.dataset import PasteVolumeDataset
from ml.paste_volume.index import (
    SPLIT_DIMENSIONS,
    PasteVolumeSampleIndex,
    SplitDimension,
)
from ml.training.data import TrainingData
from ml.training.task import GaussianObservation, StepResult, TrainingTask

# LeaveOneGroupOutPlan へ渡す次元名。fold ごとの乱数種の材料に入る。
_SESSION_DIMENSION = "session"

# 真値 0 の部分集団の metric へ付ける接頭辞。
#
# ZeroTargetMetrics は sample_count / weight_sum / mean_absolute_error /
# one_standard_deviation_coverage を GaussianRegressionMetrics と同じ名前で持つ。
# 接頭辞なしで同じ写像へ入れると、blank 16 件の値が全体の値を黙って上書きする。
_ZERO_TARGET_PREFIX = "zero_target_"


@attrs.frozen
class PasteVolumeTrainingConfig:
    """学習データの取り回し設定.

    既定は session 単位の leave-one-session-out。塗布量の係数 k は session ごとの
    1 定数なので、cell 単位で分けると model が session を言い当てて k を憶えるだけで
    見かけの精度が出る。session をまたいだ汎化はそれでは測れない。

    ``ratios`` を使う cell 単位 split も残してある。素朴ベースラインを両方の次元で
    測ると、その差そのものが「cell split では k を憶えられる」ことの実測証拠になる。

    学習の乱数種は持たない。``PasteVolumeCollator.global_seed`` が唯一の出典で、
    幾何変換・view の間引き・配置・batch の並べ替えがすべてそこから決まる。
    二重に持つと片方だけ変えたときに何が変わるのか説明できなくなる。
    """

    split_dimension: SplitDimension = "session"
    held_out_session: str | None = None
    """Session 次元での held-out 指定。label 完全一致か fingerprint の前頭一致."""

    validation_ratio: float = 0.15
    """Session 次元で、held-out を除いた残りから validation へ回す割合."""

    ratios: SplitRatios = SplitRatios(0.70, 0.15, 0.15)
    """Cell 次元での train / validation / test の配分."""

    split_seed: int = 0
    max_batch_pixels: int = 8_388_608
    max_batch_size: int = 32

    def validate(self) -> str | None:
        """設定の整合を返す.

        次元とその次元でしか意味を持たない設定の食い違いを拒む。session 次元で held-out を省くと「どの session
        を外したのか」が記録に残らないまま 5 fold の 1 つが選ばれてしまい、cell 次元で held-out
        を渡すと指定が黙って無視される。

        契約外の次元もここで弾く。型では ``SplitDimension`` に絞ってあるが、TOML や
        argv から組む経路は文字列で入ってくるので、通すと ``sample_groups`` が
        ``None`` を返して ``TypeError`` になり、理由の文字列にならない。
        """

        if self.split_dimension not in SPLIT_DIMENSIONS:
            return (
                f"split_dimension は {list(SPLIT_DIMENSIONS)} のいずれかが必要です: "
                f"{self.split_dimension!r}"
            )
        if self.split_dimension == "session" and self.held_out_session is None:
            return "session 次元の split には held_out_session の指定が必要です"
        if self.split_dimension == "cell" and self.held_out_session is not None:
            return (
                "cell 次元の split では held_out_session を使いません: "
                f"{self.held_out_session!r}"
            )
        if not math.isfinite(self.validation_ratio) or not (
            0 < self.validation_ratio < 1
        ):
            return f"validation_ratio は 0 と 1 の間が必要です: {self.validation_ratio}"
        if error := self.ratios.validate():
            return error
        if self.max_batch_pixels < 1:
            return f"max_batch_pixels は正の値が必要です: {self.max_batch_pixels}"
        if self.max_batch_size < 1:
            return f"max_batch_size は正の値が必要です: {self.max_batch_size}"
        return None


class PasteVolumeTrainingData(TrainingData[PasteVolumeBatch]):
    """読み出し・詰め込み・split の割り当てを Trainer の契約へ繋ぐ."""

    def __init__(
        self,
        dataset: PasteVolumeDataset,
        *,
        collator: PasteVolumeCollator,
        config: PasteVolumeTrainingConfig,
        split_manifest: SplitManifest,
    ) -> None:
        self._dataset = dataset
        self._collator = collator
        self._config = config
        self._split_manifest = split_manifest

    @classmethod
    def build(
        cls,
        index: PasteVolumeSampleIndex,
        *,
        collator: PasteVolumeCollator,
        config: PasteVolumeTrainingConfig,
        split_manifest_path: Path | None = None,
    ) -> tuple[Self | None, str | None]:
        """設定を検証し、split を決めて組み立てる.

        ``augmentation`` が元画像を下限より小さくしうる設定はここで弾く。通してしまうと
        ``materialize`` の途中で sample が落ち、``plan_epoch`` の計画と食い違う。

        index と collator の ``constraints`` が一致することも要求する。index はこの制約で
        使えない cell を隔離しているので、collator 側が違う制約を使うと「拒否は index を
        作る時点で済ませる」という前提が崩れる。緩いと materialize で落ち、厳しいと母集団が
        黙って減る。

        split が空でないことは確かめ直さない。新規に作る場合、cell 次元は
        ``SplitManifest.build`` が ``require_test=True`` のとき各 split へ最低 1 group を
        割り当て、group が 3 個に満たなければ理由を返す。session 次元は
        ``SplitManifest.build`` を通らず、``LeaveOneGroupOutPlan`` が held-out 1 group と
        train / validation 各 1 group 以上を保証し、満たせなければ理由を返す。
        既存 manifest を読む場合は ``validate`` が group の取りこぼしを検出する。
        """

        if error := config.validate():
            return None, error
        if error := collator.validate():
            return None, error
        if collator.constraints != index.constraints:
            return None, (
                "index と collator の constraints が違います: "
                f"{index.constraints} と {collator.constraints}"
            )
        if error := collator.constraints.validate_augmentation(
            collator.augmentation, smallest_source_size=index.smallest_source_size
        ):
            return None, error
        manifest, error = _resolve_split(index, config=config, path=split_manifest_path)
        if manifest is None:
            return None, error
        return (
            cls(
                PasteVolumeDataset(index),
                collator=collator,
                config=config,
                split_manifest=manifest,
            ),
            None,
        )

    @property
    def index(self) -> PasteVolumeSampleIndex:
        """元になった sample index."""

        return self._dataset.index

    @property
    def collator(self) -> PasteVolumeCollator:
        """詰め込みを担う collator."""

        return self._collator

    @property
    def split_manifest(self) -> SplitManifest:
        """使っている split の割り当て."""

        return self._split_manifest

    @property
    def split_dimension(self) -> SplitDimension:
        """Split の不可分単位に使った次元."""

        return self._config.split_dimension

    @property
    @override
    def dataset_fingerprint(self) -> str:
        return self.index.dataset_fingerprint

    def sample_ids_for(self, split: SplitName) -> tuple[str, ...]:
        """その split に属する sample ID."""

        return self._split_manifest.sample_ids_for(split)

    @override
    def plan_epoch(
        self, *, split: SplitName, epoch: int
    ) -> tuple[tuple[str, ...], ...]:
        training = _is_training(split)
        shapes = []
        for sample_id in self.sample_ids_for(split):
            entry = self.index.entry_for(sample_id)
            shape = self._collator.preprocessed_shape(
                entry, training=training, epoch=epoch
            )
            shapes.append(
                BatchShape(
                    sample_id=sample_id,
                    height=shape.height,
                    width=shape.width,
                    view_count=entry.view_count,
                )
            )
        return plan_pixel_budget_batches(
            shapes,
            max_batch_pixels=self._config.max_batch_pixels,
            max_batch_size=self._config.max_batch_size,
            stride=self._collator.constraints.stride,
            seed=self._collator.global_seed,
            epoch=epoch,
        )

    @override
    def materialize(
        self,
        sample_ids: Sequence[str],
        *,
        split: SplitName,
        epoch: int,
        training: bool,
        device: torch.device,
    ) -> PasteVolumeBatch:
        if training != _is_training(split):
            raise ValueError(
                "training と split が食い違っています: "
                f"training={training}、split={split}"
            )
        return self._collator.collate(
            [self._dataset.sample(sample_id) for sample_id in sample_ids],
            epoch=epoch,
            training=training,
            device=device,
        )


class PasteVolumeTask(TrainingTask[PasteVolumeBatch, GaussianObservation]):
    """多視点 batch を重み付き Gaussian negative log likelihood で学習する.

    compile 済み callable は別の属性に持ち、:attr:`model` は常に元の module を返す。

    こうしないと ``state_dict()`` のキーへ ``_orig_mod.`` が混ざり、checkpoint と export の
    公開契約が壊れる。
    """

    def __init__(self, model: MultiViewGaussianRegressor) -> None:
        self._model = model
        self._forward: Callable[..., tuple[Tensor, Tensor]] = model

    @property
    @override
    def model(self) -> nn.Module:
        """Compile 前の本体 module."""

        return self._model

    @override
    def training_step(self, batch: PasteVolumeBatch) -> StepResult[GaussianObservation]:
        """微分可能な negative log likelihood と、detach 済み観測値を返す.

        batch の形は検算しない。

        :meth:`~ml.paste_volume.batch.PasteVolumeCollator.collate` が構築の仕方で形を
        決めていて、食い違いは model の入力検査が捕まえる。同じ不整合に検出器を 2 つ置くと
        前段が後段を隠して回帰に気付けなくなる。
        """

        mean, log_variance = self._forward(
            batch.images, batch.valid_pixel_mask, batch.conditioning
        )
        loss = weighted_gaussian_negative_log_likelihood(
            mean, log_variance, batch.target, batch.sample_weight
        )
        return StepResult(
            loss=loss,
            observation=_observation_of(batch, mean, log_variance),
            sample_count=int(batch.images.shape[0]),
        )

    @override
    def evaluation_step(self, batch: PasteVolumeBatch) -> GaussianObservation:
        """勾配を作らずに予測と実測を返す."""

        with torch.no_grad():
            mean, log_variance = self._forward(
                batch.images, batch.valid_pixel_mask, batch.conditioning
            )
        return _observation_of(batch, mean, log_variance)

    @override
    def reduce(
        self, observations: Sequence[GaussianObservation]
    ) -> Mapping[str, float]:
        """観測値を連結し、回帰 metric と 2 つの診断を返す.

        :class:`~ml.evaluation.regression.MeanSaturationDiagnostic` と
        :class:`~ml.evaluation.regression.ZeroTargetMetrics` は主要 metric が測れなくても
        返す。

        平均 head の ReLU が全 sample で 0 に張り付くと
        :meth:`~ml.evaluation.regression.GaussianPredictions.valid_sample_mask` が
        ``mean > 0`` を要求するため回帰 metric を 1 つも出せず、空の写像だけを返すと
        運用者が受け取るのは Trainer の「monitor がありません」になって真の原因が読めない。

        真値 0 の集団は :class:`~ml.evaluation.regression.GaussianRegressionMetrics` に
        一切現れないので、blank 16 件はここが唯一の観測点になる。

        主要 monitor を欠かせること自体は変えない。評価できない run は Trainer が
        従来どおり止める。
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
        zero_target, _ = ZeroTargetMetrics.measure(predictions)
        if zero_target is not None:
            values.update(_as_float_mapping(zero_target, prefix=_ZERO_TARGET_PREFIX))
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


def _observation_of(
    batch: PasteVolumeBatch, mean: Tensor, log_variance: Tensor
) -> GaussianObservation:
    """計算グラフを切り離した観測値へ束ねる.

    ``sample_ids`` は載せない。

    :class:`~ml.training.task.GaussianObservation` は ``ml`` 側の型で、session ごとの
    slice は evaluate 側が split manifest から引き直す。
    """

    return GaussianObservation(
        mean=mean.detach(),
        log_variance=log_variance.detach(),
        target=batch.target.detach(),
        sample_weight=batch.sample_weight.detach(),
    )


def _as_float_mapping(
    record: attrs.AttrsInstance, *, prefix: str = ""
) -> dict[str, float]:
    """Frozen な集計結果を、logger へ渡せる float の写像へ落とす."""

    return {
        f"{prefix}{name}": float(cast(float, value))
        for name, value in attrs.asdict(record).items()
    }


def _is_training(split: SplitName) -> bool:
    """その split を学習として扱うか.

    ``plan_epoch`` は split しか受け取らないので、augmentation の有無を split から
    決めるほかない。``materialize`` の ``training`` がこれと食い違うと、計画時と実際の
    shape がずれる。``ml.training.loop`` は train を ``training=True``、validation を
    ``training=False`` で実体化する。
    """

    return split == "train"


def _resolve_split(
    index: PasteVolumeSampleIndex,
    *,
    config: PasteVolumeTrainingConfig,
    path: Path | None,
) -> tuple[SplitManifest | None, str | None]:
    """既存の manifest を読むか、無ければ設定した次元で作る.

    読み直しの検証も同じ次元の group で行う。``SplitManifest.validate`` は 1 group が
    複数 split にまたがることを拒むので、cell group で作った manifest を session 次元で
    読み直すと必ず落ちる。2 つの次元は入れ子ではなく直交していて、cell group はどれも
    全 session の sample を含むため。

    session 次元では、それに加えて held-out の一致も見る。``SplitManifest`` は次元も
    held-out も持たないので、別の fold の manifest でも ``validate`` は何も言わない。
    """

    groups = index.sample_groups(dimension=config.split_dimension)
    if path is not None and path.is_file():
        manifest, error = SplitManifest.load(
            path, dataset_fingerprint=index.dataset_fingerprint
        )
        if manifest is None:
            return None, error
        if error := manifest.validate(
            groups, dataset_fingerprint=index.dataset_fingerprint
        ):
            return None, error
        if error := _held_out_mismatch(
            index, config=config, groups=groups, manifest=manifest
        ):
            return None, error
        return manifest, None
    manifest, error = _built_split(index, config=config, groups=groups)
    if manifest is None:
        return None, error
    if path is not None:
        manifest.save(path)
    return manifest, None


def _built_split(
    index: PasteVolumeSampleIndex,
    *,
    config: PasteVolumeTrainingConfig,
    groups: Mapping[str, str],
) -> tuple[SplitManifest | None, str | None]:
    """設定した次元で split を 1 つ作る."""

    match config.split_dimension:
        case "session":
            # 空文字は resolve_session が「指定が空です」で落とす。config.validate も
            # None を弾いているので、ここで同じ理由を二重に書かない
            return _leave_one_session_out_manifest(
                index,
                groups=groups,
                held_out=config.held_out_session or "",
                seed=config.split_seed,
                validation_ratio=config.validation_ratio,
            )
        case "cell":
            return SplitManifest.build(
                groups,
                dataset_fingerprint=index.dataset_fingerprint,
                seed=config.split_seed,
                ratios=config.ratios,
                require_test=True,
            )


def _leave_one_session_out_manifest(
    index: PasteVolumeSampleIndex,
    *,
    groups: Mapping[str, str],
    held_out: str,
    seed: int,
    validation_ratio: float,
) -> tuple[SplitManifest | None, str | None]:
    """1 session を test に固定した split を作る.

    ``groups`` は呼び出し側が作った session 次元の ``{sample_id: group}``。ここで
    作り直すと、次元をずらす変異が 1 箇所では効かなくなる。

    ``LeaveOneGroupOutPlan`` が使えない（session が 2 本以下）ときは sample 単位へ
    fallback せず理由を返す。fallback すると、session をまたいだ汎化を測っている
    つもりの run が黙って別のものを測る。
    """

    fingerprint, reason = index.resolve_session(held_out)
    if fingerprint is None:
        return None, reason
    values = index.session_values()
    plan = LeaveOneGroupOutPlan.build(
        {value: value for value in values},
        dimension=_SESSION_DIMENSION,
        seed=seed,
        validation_ratio=validation_ratio,
    )
    if not plan.available:
        return None, plan.reason
    fold = {item.held_out_value: item for item in plan.folds}[fingerprint]
    return (
        SplitManifest(
            dataset_fingerprint=index.dataset_fingerprint,
            seed=fold.seed,
            train_sample_ids=_sample_ids_of(groups, fold.train_group_ids),
            validation_sample_ids=_sample_ids_of(groups, fold.validation_group_ids),
            test_sample_ids=_sample_ids_of(groups, fold.held_out_group_ids),
        ),
        None,
    )


def _held_out_mismatch(
    index: PasteVolumeSampleIndex,
    *,
    config: PasteVolumeTrainingConfig,
    groups: Mapping[str, str],
    manifest: SplitManifest,
) -> str | None:
    """読み直した manifest の test split が、要求した held-out session と一致するか.

    session 次元でだけ見る。``SplitManifest`` は次元も held-out も持たないので、
    session-0 の fold で作った split.json は session-2 を要求した build も素通りし、
    要求した session が train へ入ったまま run が進む。``run_directory`` の既定は
    fold 間で共有され得るので、report まで誰も気づけない。
    """

    if config.split_dimension != "session":
        return None
    fingerprint, reason = index.resolve_session(config.held_out_session or "")
    if fingerprint is None:
        return reason
    requested = frozenset(_sample_ids_of(groups, (fingerprint,)))
    if frozenset(manifest.test_sample_ids) != requested:
        return (
            "既存の split manifest の test split が held_out_session と一致しません: "
            f"{config.held_out_session!r}（test {len(manifest.test_sample_ids)} sample、"
            f"要求した session {len(requested)} sample）"
        )
    return None


def _sample_ids_of(
    groups: Mapping[str, str], selected: Sequence[str]
) -> tuple[str, ...]:
    chosen = frozenset(selected)
    return tuple(
        sorted(sample_id for sample_id, group in groups.items() if group in chosen)
    )


__all__ = [
    "PasteVolumeTask",
    "PasteVolumeTrainingConfig",
    "PasteVolumeTrainingData",
]
