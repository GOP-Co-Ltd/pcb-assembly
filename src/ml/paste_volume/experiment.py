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
from typing import Self
from urllib.parse import urlsplit

import attrs

from ml.artifact.document import DocumentKind
from ml.config.composition import ConfigComposition
from ml.config.packaged import PackagedConfiguration
from ml.data.batch import ViewDropout
from ml.data.image import AugmentationRange, ImageConstraints
from ml.data.split import SplitName, SplitRatios
from ml.experiment.logger import ExperimentLogger
from ml.experiment.mlflow import (
    MLflowExperimentLogger,
    MLflowRunTarget,
    database_backend_scheme,
)
from ml.experiment.provenance import sanitize_persisted_uri
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

# 1 run の成果物を置く directory の中身（仕様書 §5）。
#
# ``latest.pt`` / ``best.pt`` / ``final.pt`` / ``emergency.pt`` は
# :class:`~ml.training.checkpoint.CheckpointStore` が同じ directory へ書く。
CONFIG_FILE_NAME = "config.json"
SPLIT_FILE_NAME = "split.json"
WEIGHTS_FILE_NAME = "weights.pt"
CALIBRATION_FILE_NAME = "calibration.json"
GIT_DIFF_FILE_NAME = "git-diff.patch"

# 成果物の置き場所として受け付ける scheme のうち、local path として扱うもの。
#
# それ以外（``s3:`` など）の remote store は path の絶対性を問わない。
_LOCAL_ARTIFACT_SCHEMES = ("", "file")

# 解決済み config を run directory と MLflow の両方へ残すための封筒。
EXPERIMENT_CONFIG_DOCUMENT = DocumentKind(
    kind="paste-volume-experiment-config", schema_version=1
)

# 学習後に validation split だけで fit した log 分散 offset の封筒。
CALIBRATION_DOCUMENT = DocumentKind(
    kind="paste-volume-uncertainty-calibration", schema_version=1
)

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
class UncertaintyCalibration:
    """Validation split だけで fit した log 分散への scalar offset.

    平均は変えない。

    offset を足す前後の 1 標準偏差 coverage を両方持つのは、calibration が効いたかどうかを run
    記録だけで読めるようにするため。

    書くのは学習 entrypoint、読むのは評価 entrypoint。封筒（``calibration.json``）を
    置いているこの module が両者の共通の下流になる。
    """

    split: SplitName
    sample_count: int
    log_variance_offset: float
    coverage_before: float
    coverage_after: float

    def save(self, path: Path) -> None:
        """封筒付き JSON として書き出す."""

        CALIBRATION_DOCUMENT.save(path, self, converter=make_strict_converter())

    @classmethod
    def load(cls, path: Path) -> tuple[Self | None, str | None]:
        """書き出した calibration を読み戻す."""

        return CALIBRATION_DOCUMENT.load(path, cls, converter=make_strict_converter())


@attrs.frozen
class ExperimentLoggerConfig:
    """記録先の MLflow.

    ``tracking_uri`` は同梱 conf へ書かない。

    相対 URI を既定にすると起動した directory ごとに store が分かれ、5 fold の
    run が別々の場所へ散る。
    """

    tracking_uri: str
    experiment_name: str

    artifact_location: str | None = None
    """成果物の置き場所。database backend では必須."""

    def validate(self) -> str | None:
        """記録先の指定が揃っているかを返す.

        Client が直接 database を開く tracking store では、成果物の置き場所を
        要求する。

        指定しないと MLflow は experiment を作るときに現在 directory の相対
        path（``./mlruns``）を焼き付ける。起こした directory ごとに成果物が
        散り、あとから run を開いても artifact を辿れない。これは sqlite に
        限らず MLflow の database backend すべてに掛かる。

        Server の tracking URI では要求しない。置き場所は server 側の設定で、
        client が渡す値ではない。

        使えない tracking URI（filesystem backend）は
        :meth:`~ml.experiment.mlflow.MLflowRunTarget.validate` が落とす。
        """

        if error := self.run_target().validate():
            return error
        scheme = database_backend_scheme(self.tracking_uri)
        if scheme is not None and self.artifact_location is None:
            return (
                f"{scheme} の tracking URI には logger.artifact_location "
                f"（絶対 path）が必要です: {self.tracking_uri}"
            )
        if self.artifact_location is not None and not _absolute_location(
            self.artifact_location
        ):
            return (
                "logger.artifact_location は絶対 path が必要です: "
                f"{self.artifact_location!r}"
            )
        return None

    def run_target(
        self, *, run_name: str | None = None, resume_run_id: str | None = None
    ) -> MLflowRunTarget:
        """MLflow adapter へ渡す宛先へ写す.

        秘匿済み URI を param へ載せるのも run 側の仕事なので公開する。

        ``resume_run_id`` は checkpoint から続ける run の識別子。

        :class:`~ml.training.loop.Trainer` は resume 元の checkpoint と
        logger の run_id が食い違うと拒否するので、新しい run を開くと
        再開そのものが失敗する。

        ``run_name`` は 1 プロセスが複数 run を起こす探索でだけ使う。
        """

        return MLflowRunTarget(
            tracking_uri=self.tracking_uri,
            experiment_name=self.experiment_name,
            run_name=run_name,
            resume_run_id=resume_run_id,
            artifact_location=self.artifact_location,
        )

    def build(
        self, *, run_name: str | None = None, resume_run_id: str | None = None
    ) -> ExperimentLogger:
        """記録先へ書く logger を作る."""

        return MLflowExperimentLogger(
            self.run_target(run_name=run_name, resume_run_id=resume_run_id)
        )


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


def save_experiment_config(config: PasteVolumeExperimentConfig, path: Path) -> None:
    """解決済み config を封筒付き JSON として書き出す.

    合成の経路（argv・group 層・既定値）はここには残らない。

    残すのは「その run が実際に使った値」で、run を読み直す側が argv を再現しなくても同じ model と split
    を組み立てられるようにする。

    外部システムの URI は credential を落としてから書く。

    この file は run directory に残るうえ MLflow の artifact としても上がるので、
    ``postgresql://user:pw@host/db`` のような URI をそのまま持たせると password が
    成果物として配られる。同じ規約を param（``sanitized_tracking_uri``）と
    study 成果物（``storage_uri_redacted``）が既に使っている。
    """

    EXPERIMENT_CONFIG_DOCUMENT.save(
        path, redacted_experiment_config(config), converter=make_strict_converter()
    )


def redacted_experiment_config(
    config: PasteVolumeExperimentConfig,
) -> PasteVolumeExperimentConfig:
    """外部システムの URI から credential を落とした config を返す.

    落とすのは credential と query / fragment だけなので、成果物からでも記録先と storage
    の所在は読める。
    """

    logger = config.logger
    search = config.hyperparameter_search
    return attrs.evolve(
        config,
        logger=(
            None
            if logger is None
            else attrs.evolve(
                logger, tracking_uri=sanitize_persisted_uri(logger.tracking_uri)
            )
        ),
        hyperparameter_search=(
            None
            if search is None
            else attrs.evolve(
                search, storage_uri=sanitize_persisted_uri(search.storage_uri)
            )
        ),
    )


def _absolute_location(location: str) -> bool:
    """成果物の置き場所が絶対 path（か remote store の URI）かを返す."""

    parsed = urlsplit(location)
    if parsed.scheme not in _LOCAL_ARTIFACT_SCHEMES:
        return True
    path = parsed.path if parsed.scheme else location
    return path.startswith("/")


def load_experiment_config(
    path: Path,
) -> tuple[PasteVolumeExperimentConfig | None, str | None]:
    """書き出した解決済み config を読み戻す."""

    return EXPERIMENT_CONFIG_DOCUMENT.load(
        path, PasteVolumeExperimentConfig, converter=make_strict_converter()
    )


__all__ = [
    "BASE_LAYER_NAMES",
    "CALIBRATION_DOCUMENT",
    "CALIBRATION_FILE_NAME",
    "CONFIG_FILE_NAME",
    "EXPERIMENT_CONFIG_DOCUMENT",
    "GIT_DIFF_FILE_NAME",
    "SPLIT_FILE_NAME",
    "WEIGHTS_FILE_NAME",
    "ExperimentLoggerConfig",
    "PasteVolumeDataConfig",
    "PasteVolumeExperimentConfig",
    "ResumeConfig",
    "SearchConfig",
    "UncertaintyCalibration",
    "compose_experiment",
    "load_experiment_config",
    "packaged_configuration",
    "redacted_experiment_config",
    "save_experiment_config",
]
