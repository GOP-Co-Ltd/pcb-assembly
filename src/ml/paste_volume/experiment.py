"""同梱 TOML と argv から 1 run ぶんの設定を組み立てる境界.

層の merge と ``key=value`` の解釈は
:class:`~ml.config.composition.ConfigComposition` の 1 本を使う。

ここが持つのは到達先の frozen attrs と同梱 conf の所在だけで、TOML の
読み方も上書きの規則も複製しない。

同梱 conf は外部システムの所在を持たない。

dataset root・MLflow の tracking URI・Optuna storage・resume 元・初期 weight
はいずれも機械固有なので、既定値を持たない必須 field にして argv から渡させる。

分割の次元も同梱 conf の ``experiment`` group だけが宣言する。

塗布量の係数 k は session ごとの 1 定数なので、cell 単位で分けると model が
session を言い当てて k を憶えるだけで見かけの精度が出る。どちらの次元で
汎化を測るのかが既定値として隠れてはならない。

学習 core はこの module を import しない（設定合成層は entrypoint 側の層）。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import attrs

from ml.config.composition import ConfigComposition
from ml.config.packaged import PackagedConfiguration
from ml.data.batch import ViewDropout
from ml.data.image import AugmentationRange, ImageConstraints
from ml.data.split import SplitRatios
from ml.experiment.logger import ExperimentLogger
from ml.experiment.mlflow import MLflowExperimentLogger, MLflowRunTarget
from ml.paste_volume.batch import PasteVolumeCollator
from ml.paste_volume.index import SplitDimension
from ml.paste_volume.model import PasteVolumeModelConfig
from ml.paste_volume.task import PasteVolumeTrainingConfig
from ml.serialization import make_strict_converter
from ml.training.loop import TrainerConfig
from ml.tuning.search_space import SearchSpace
from ml.tuning.study import Direction, StudyStorage

_CONFIGURATION_DIRECTORY_NAME = "conf"

# ``ConfigComposition`` が group 層より先に積む層。
BASE_LAYER_NAMES: tuple[str, ...] = ("base",)

# 学習データの取り回しの既定値の出典。
#
# 同じ値を 2 つの attrs へ書くと、片方だけ変えたときに TOML も argv も
# 変わっていないのに挙動が変わる。既定値は
# :class:`~ml.paste_volume.task.PasteVolumeTrainingConfig` にのみ置き、
# ここは参照するだけとする（一致は test_conf.py が機械検証する）。
_TRAINING_DEFAULTS = PasteVolumeTrainingConfig()


def packaged_configuration() -> PackagedConfiguration:
    """このドメインに同梱した設定 root を指す.

    :meth:`~ml.config.packaged.PackagedConfiguration.locate` は ``ml.config``
    自身の ``conf`` を指すので使えない。
    """

    return PackagedConfiguration(
        root=Path(__file__).parent / _CONFIGURATION_DIRECTORY_NAME
    )


@attrs.frozen
class PasteVolumeDataConfig:
    """収集 session の読み出しと split の設定.

    ``split_dimension`` は既定値を持たない。

    ``experiment`` group の option file が宣言する唯一の場所で、preset を選ばずに
    起こすと必須 field の欠落で run 開始前に落ちる。
    """

    roots: tuple[Path, ...]
    """収集 session を探す root。機械固有なので argv で渡す."""

    split_dimension: SplitDimension
    """汎化を測る次元。``experiment`` group が宣言する."""

    constraints: ImageConstraints = ImageConstraints()
    augmentation: AugmentationRange = AugmentationRange()
    view_dropout: ViewDropout = ViewDropout()
    global_seed: int = 0
    held_out_session: str | None = _TRAINING_DEFAULTS.held_out_session
    validation_ratio: float = _TRAINING_DEFAULTS.validation_ratio
    ratios: SplitRatios = _TRAINING_DEFAULTS.ratios
    split_seed: int = _TRAINING_DEFAULTS.split_seed
    max_batch_pixels: int = _TRAINING_DEFAULTS.max_batch_pixels
    max_batch_size: int = _TRAINING_DEFAULTS.max_batch_size

    def validate(self) -> str | None:
        """設定の整合を返す.

        画像制約と augmentation の組み合わせのうち、源画像の最小辺を要する検査
        （:meth:`~ml.data.image.ImageConstraints.validate_augmentation`）は
        ここでは行わない。

        必要な ``smallest_source_size`` は index を組んだあとでしか分からない。
        """

        if not self.roots:
            return 'data.roots が空です（data.roots=["/abs/..."] を指定してください）'
        if error := self.collator().validate():
            return error
        return self.training_config().validate()

    def collator(self) -> PasteVolumeCollator:
        """前処理と詰め込みの設定へ写す."""

        return PasteVolumeCollator(
            constraints=self.constraints,
            augmentation=self.augmentation,
            view_dropout=self.view_dropout,
            global_seed=self.global_seed,
        )

    def training_config(self) -> PasteVolumeTrainingConfig:
        """分割と batch の設定へ写す."""

        return PasteVolumeTrainingConfig(
            split_dimension=self.split_dimension,
            held_out_session=self.held_out_session,
            validation_ratio=self.validation_ratio,
            ratios=self.ratios,
            split_seed=self.split_seed,
            max_batch_pixels=self.max_batch_pixels,
            max_batch_size=self.max_batch_size,
        )


@attrs.frozen
class ExperimentLoggerConfig:
    """記録先の MLflow.

    ``tracking_uri`` は同梱 conf へ書かない。

    相対 URI を既定にすると起動した directory ごとに store が分かれ、5 fold の
    run が別々の場所へ散る。
    """

    tracking_uri: str
    experiment_name: str

    def validate(self) -> str | None:
        """記録先の指定が揃っているかを返す."""

        return self.run_target().validate()

    def run_target(self) -> MLflowRunTarget:
        """MLflow adapter へ渡す宛先へ写す.

        秘匿済み URI を param へ載せるのも run 側の仕事なので公開する。
        """

        return MLflowRunTarget(
            tracking_uri=self.tracking_uri, experiment_name=self.experiment_name
        )

    def build(self) -> ExperimentLogger:
        """記録先へ書く logger を作る."""

        return MLflowExperimentLogger(self.run_target())


@attrs.frozen
class ResumeConfig:
    """中断した run の再開点.

    ``resume.checkpoint`` は「同じ run の続き」、``model.initial_weights`` は
    「別 run の weight を起点に新しい run を始める」。

    前者は dataset と config の fingerprint 一致を要求し、後者は要求しない。
    混ぜると、別条件で学んだ weight を同じ run の続きとして記録してしまう。
    """

    checkpoint: Path | None = None


@attrs.frozen
class SearchConfig:
    """1 プロセスぶんの Optuna 探索設定.

    ``storage_uri`` は同梱 conf へ書かない。

    sqlite は絶対 path を要求し、server 形式は credential を含むので、どちらも
    機械固有の値になる。
    """

    storage_uri: str
    search_space: SearchSpace
    trial_count: int = 20
    direction: Direction = "minimize"
    results_path: Path | None = None

    def validate(self) -> str | None:
        """探索の設定が成立しているかを返す.

        ``direction`` の contract は strict converter が守る（Literal の別名なので
        契約外の文字列は構造化で落ちる）。同じ不整合へ検出器を 2 つ置かない。
        """

        if error := self.storage().validate():
            return error
        if error := self.search_space.validate():
            return error
        if self.trial_count < 1:
            return f"trial_count は正の整数が必要です: {self.trial_count}"
        return None

    def storage(self) -> StudyStorage:
        """Trial を共有する storage へ写す."""

        return StudyStorage(uri=self.storage_uri)


@attrs.frozen
class PasteVolumeExperimentConfig:
    """学習と探索の entrypoint が argv から組み立てる root config."""

    data: PasteVolumeDataConfig
    trainer: TrainerConfig
    model: PasteVolumeModelConfig = PasteVolumeModelConfig()
    logger: ExperimentLoggerConfig | None = None
    resume: ResumeConfig = ResumeConfig()
    hyperparameter_search: SearchConfig | None = None
    run_kind: str = "base-train"
    run_directory: Path = Path("runs")

    def validate(self) -> str | None:
        """全 group の整合と、group をまたぐ整合を返す.

        model と画像制約の組み合わせだけは 1 つの group では見られない。

        総 stride が前処理の最小サイズを超えると特徴 map が消える。
        """

        if error := self.data.validate():
            return f"data: {error}"
        if error := self.trainer.validate():
            return f"trainer: {error}"
        if error := self.model.validate_for_constraints(self.data.constraints):
            return f"model: {error}"
        if self.logger is not None and (error := self.logger.validate()):
            return f"logger: {error}"
        if self.hyperparameter_search is not None and (
            error := self.hyperparameter_search.validate()
        ):
            return f"hyperparameter_search: {error}"
        if not self.run_kind:
            return "run_kind は空にできません"
        return None


def compose_experiment(
    arguments: Sequence[str],
) -> tuple[PasteVolumeExperimentConfig | None, str | None]:
    """同梱 conf の配下で argv を合成し、strict に構造化する.

    :meth:`PasteVolumeExperimentConfig.validate` は呼ばない。

    構造化できたことと、その設定で学習を始めてよいことは別の判定で、
    entrypoint が理由をどう報告するかを決める。
    """

    located = packaged_configuration()
    if error := located.validate():
        return None, error
    composition, error = ConfigComposition.from_arguments(
        arguments, configuration_root=located.root, base_names=BASE_LAYER_NAMES
    )
    if composition is None:
        return None, error
    return composition.structure(
        PasteVolumeExperimentConfig, converter=make_strict_converter()
    )


__all__ = [
    "BASE_LAYER_NAMES",
    "ExperimentLoggerConfig",
    "PasteVolumeDataConfig",
    "PasteVolumeExperimentConfig",
    "ResumeConfig",
    "SearchConfig",
    "compose_experiment",
    "packaged_configuration",
]
