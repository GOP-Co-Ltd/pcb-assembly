"""Argv を所有する塗布量推定の学習 entrypoint.

1 プロセス = 1 fold = 1 実験 run とする。

:class:`~ml.training.loop.Trainer` も
:class:`~ml.training.checkpoint.CheckpointStore` も MLflow run も「1 run」を
単位に resume と成果物を組んでいるので、5 fold を 1 プロセスで回すとどれも
単位が壊れる。

5 fold は shell の for ループから起こす。

.. code-block:: shell

    set -e
    for S in "${SESSIONS[@]}"; do
      uv run python -m ml.paste_volume.train \
          experiment=base trainer=gpu logger=mlflow \
          data.roots='["/abs/data/paste-volume-datasets"]' \
          data.held_out_session="$S" \
          logger.tracking_uri="sqlite:////abs/mlflow.db" \
          logger.artifact_location="/abs/mlartifacts" \
          run_directory="/abs/runs/loso/$S"
    done

``run_directory`` は fold ごとに別の値を渡す。

打ち切られた fold で ``set -e`` のループが止まるよう、signal / deadline で
止まった run は :data:`INTERRUPTED_EXIT_CODE` を返す。

split.json は run directory に残り、次の run が同じ directory を指すと再利用
される。fold をまたいで共有すると、要求した held-out と実際の test split が
食い違ったまま run が進む形になる（検査は入っているが、run を分けるほうが
先に効く）。

学習 core はこの module を import しない。
"""

from __future__ import annotations

import statistics
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Self, override

import attrs
import torch

from ml.artifact.atomic import atomic_write_stream, atomic_write_text
from ml.artifact.document import DocumentKind
from ml.artifact.fingerprint import fingerprint_json
from ml.data.image import ImageConstraints
from ml.data.split import SplitManifest, SplitName
from ml.evaluation.regression import GaussianRegressionMetrics
from ml.experiment.logger import (
    ExperimentLogger,
    Scalar,
    TaggedExperimentLogger,
)
from ml.experiment.provenance import DependencyVersions, GitProvenance
from ml.model.inspection import ModelSize
from ml.model.multiview import MultiViewGaussianRegressor
from ml.paste_volume.experiment import (
    CALIBRATION_FILE_NAME,
    CONFIG_FILE_NAME,
    GIT_DIFF_FILE_NAME,
    SPLIT_FILE_NAME,
    WEIGHTS_FILE_NAME,
    PasteVolumeExperimentConfig,
    compose_experiment,
    save_experiment_config,
)
from ml.paste_volume.index import PasteVolumeSampleIndex
from ml.paste_volume.model import (
    MODEL_FAMILY,
    PasteVolumeModelConfig,
    apply_fine_tune_freeze,
    build_paste_volume_model,
    measure_paste_volume_model,
)
from ml.paste_volume.task import (
    PasteVolumeTask,
    PasteVolumeTrainingData,
    collect_predictions,
)
from ml.serialization import make_strict_converter, structure_strictly
from ml.training.checkpoint import CheckpointStore
from ml.training.loop import StopReason, Trainer, TrainingOutcome
from ml.training.random_state import seed_everything

# 仕様書 §2 の計算量 gate。
#
# 測る shape は 159 px x 5 view（点塗布 crop の上限）とする。仕様書 §2 はかつて
# 「512x512 で 1.5 GMAC 以下」とも書いていたが、その構成は実測 13.4 GMAC で全 run が
# 学習前に拒否される。512 は多視点化以前の単一 view 時代の記述で、
# ``ImageConstraints.maximum_size`` は前処理の契約上限であって点塗布 crop の
# 実際の上限ではない。実測は 159 px x 5 view で 1.307149 GMAC。
PARAMETER_BUDGET = 1_500_000
GIGA_MULTIPLY_ACCUMULATE_BUDGET = 1.5
BUDGET_CROP_SIZE_PX = 159
BUDGET_VIEW_COUNT = 5

# 学習後に validation split だけで fit した log 分散 offset の封筒。
CALIBRATION_DOCUMENT = DocumentKind(
    kind="paste-volume-uncertainty-calibration", schema_version=1
)

# ``weights.pt`` の payload。再開用ではなく、評価と export の入力。
WEIGHTS_KIND = "paste-volume-model-weights"
WEIGHTS_SCHEMA_VERSION = 1

# 平均 head の bias 初期値が真値スケールから外れたと報告する倍率。
#
# ``mean_bias_initial`` は fold 間で比較できるよう config 固定なので、真値の
# スケールが将来変わると誰も気づかないまま外れ続ける。
MEAN_BIAS_DEVIATION_FACTOR = 3.0

_MISSING_PARAM_VALUE = ""

# 最後まで回りきった run の停止理由。
#
# 5 fold の shell ループは、打ち切られた fold を成功として次へ進んではならない。
# 打ち切られた run の metric を report へ混ぜると、比べているものが fold ごとに
# 変わる。
COMPLETED_STOP_REASONS: frozenset[StopReason] = frozenset(
    {"max_epochs", "max_steps", "early_stopping"}
)

# 学習は動いたが最後まで行かなかった run の終了コード。
#
# argv の不備（1）と区別できるよう別の値にする。
INTERRUPTED_EXIT_CODE = 2


@attrs.frozen
class UncertaintyCalibration:
    """Validation split だけで fit した log 分散への scalar offset.

    平均は変えない。

    offset を足す前後の 1 標準偏差 coverage を両方持つのは、calibration が効いたかどうかを run
    記録だけで読めるようにするため。
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


@attrs.frozen(eq=False)
class ModelWeights:
    """``weights.pt`` の中身.

    再開用の checkpoint とは別物で、optimizer も乱数状態も持たない。

    ``fine_tune`` の起点（``model.initial_weights``）と Phase 4 の export が読む。

    Tensor を持つので等価性は identity で決める。
    """

    model_family: str
    model_config: PasteVolumeModelConfig
    constraints: ImageConstraints
    dataset_fingerprint: str
    model_state: Mapping[str, torch.Tensor]


def save_model_weights(path: Path, weights: ModelWeights) -> None:
    """Best model の重みと、model / 前処理の schema を atomic に書き出す."""

    converter = make_strict_converter()
    payload = {
        "kind": WEIGHTS_KIND,
        "schema_version": WEIGHTS_SCHEMA_VERSION,
        "model_family": weights.model_family,
        "model_config": converter.unstructure(weights.model_config),
        "constraints": converter.unstructure(weights.constraints),
        "dataset_fingerprint": weights.dataset_fingerprint,
        "model_state": {
            name: tensor.detach().cpu().clone()
            for name, tensor in weights.model_state.items()
        },
    }
    atomic_write_stream(path, lambda stream: torch.save(payload, stream))


def load_model_weights(path: Path) -> tuple[ModelWeights | None, str | None]:
    """``weights.pt`` を読み、形が違えば理由を返す."""

    if not path.is_file():
        return None, f"weights が見つかりません: {path}"
    payload = torch.load(path, map_location="cpu", weights_only=True)
    if not isinstance(payload, dict):
        return None, f"weights が写像ではありません: {path}"
    kind = payload.get("kind")
    version = payload.get("schema_version")
    if kind != WEIGHTS_KIND or version != WEIGHTS_SCHEMA_VERSION:
        return None, (
            f"未対応の weights です: kind={kind!r} schema_version={version!r}"
            f"（期待値 {WEIGHTS_KIND!r} / {WEIGHTS_SCHEMA_VERSION}）"
        )
    model_config, error = _structured(
        payload.get("model_config"), PasteVolumeModelConfig
    )
    if model_config is None:
        return None, error
    constraints, error = _structured(payload.get("constraints"), ImageConstraints)
    if constraints is None:
        return None, error
    state = payload.get("model_state")
    if not isinstance(state, dict):
        return None, f"weights に model_state がありません: {path}"
    return (
        ModelWeights(
            model_family=str(payload.get("model_family", "")),
            model_config=model_config,
            constraints=constraints,
            dataset_fingerprint=str(payload.get("dataset_fingerprint", "")),
            model_state={str(name): tensor for name, tensor in state.items()},
        ),
        None,
    )


class _StartupRecordingLogger(TaggedExperimentLogger):
    """Run を start した直後に、その run で不変の記録を必ず載せる logger.

    :meth:`~ml.training.loop.Trainer.run` が run の開始と終了を持つので、
    呼び出し側は run が開いている隙間を持たない。

    解決済み config のような「学習を始める前に決まっていて、始まった run へ
    必ず添えたいもの」をここへ預ける。
    """

    def __init__(
        self,
        inner: ExperimentLogger,
        *,
        tags: Mapping[str, str],
        params: Mapping[str, Scalar],
        artifacts: Sequence[Path],
    ) -> None:
        super().__init__(inner, tags=tags)
        self._params = dict(params)
        self._artifacts = tuple(artifacts)

    @override
    def start(
        self,
        *,
        run_kind: str,
        run_name: str | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> str:
        """Run を開始し、預かった params と成果物をその run へ載せる."""

        run_id = super().start(run_kind=run_kind, run_name=run_name, tags=tags)
        self.log_params(self._params)
        for path in self._artifacts:
            self.log_artifact(path)
        return run_id


def run_training(
    config: PasteVolumeExperimentConfig,
    *,
    logger: ExperimentLogger,
    device: torch.device | None = None,
) -> tuple[TrainingOutcome | None, str | None]:
    """1 fold ぶんの学習を最後まで回し、成果物を run directory へ残す.

    ``logger`` は必須にする。

    記録を持たない run は設定も metric も残らず、あとから「何を測ったのか」を
    誰も辿れない。無指定でも動く no-op を置くと、記録が黙って無効になった run と
    そうでない run を見分けられなくなる。

    戻り値は ``(outcome, 理由)`` で、2 つの意味を持つ。

    ``outcome`` が ``None`` なら「学習を始める前に分かる不備」で、run は 1 度も
    始まっていない。``outcome`` があるのに理由が付くときは「学習は終わったが
    成果物を 1 つ作れなかった」で、いま起きうるのは calibration の失敗だけ。
    ``calibration.json`` の offset は report に載る値なので、黙って落とさない。

    学習中の失敗（非有限 loss、resume の fingerprint 不一致）は
    :class:`~ml.training.loop.Trainer` が送出する。同じ不整合へ検出器を 2 つ
    置かない。
    """

    if error := config.validate():
        return None, error
    run_directory = config.run_directory
    run_directory.mkdir(parents=True, exist_ok=True)

    index, error = PasteVolumeSampleIndex.from_roots(
        config.data.roots, constraints=config.data.constraints
    )
    if index is None:
        return None, error
    data, error = PasteVolumeTrainingData.build(
        index,
        collator=config.data.collator(),
        config=config.data.training_config(),
        split_manifest_path=run_directory / SPLIT_FILE_NAME,
    )
    if data is None:
        return None, error

    # Model の初期化は Trainer.run() より前に起きるので、run seed をここでも
    # 撒く。撒かないと初期重みが process の周囲の乱数状態で変わり、同じ argv の
    # 2 つの run が別の重みから始まる。
    seed_everything(config.trainer.seed, deterministic=config.trainer.deterministic)
    model, error = build_paste_volume_model(config.model)
    if model is None:
        return None, error
    if config.model.initial_weights is not None:
        if error := _load_initial_weights(model, config.model.initial_weights):
            return None, error
    frozen = apply_fine_tune_freeze(model) if config.model.fine_tune else ()
    size = measure_paste_volume_model(
        model,
        height=BUDGET_CROP_SIZE_PX,
        width=BUDGET_CROP_SIZE_PX,
        view_count=BUDGET_VIEW_COUNT,
    )
    if error := _budget_rejection(size):
        return None, error

    save_experiment_config(config, run_directory / CONFIG_FILE_NAME)
    # 作業ツリーの由来は 1 度だけ取る。git diff は毎回 subprocess を起こす。
    provenance, provenance_reason = GitProvenance.capture(_repository_root())
    task = PasteVolumeTask(model)
    trainer = Trainer(
        task,
        data,
        config=config.trainer,
        store=CheckpointStore(run_directory),
        logger=_StartupRecordingLogger(
            logger,
            tags=_run_tags(
                config,
                data=data,
                run_directory=run_directory,
                provenance=provenance,
                provenance_reason=provenance_reason,
            ),
            params=_run_params(config, data=data, size=size, frozen=frozen),
            artifacts=_startup_artifacts(run_directory, provenance=provenance),
        ),
        device=device,
    )
    outcome = trainer.run(
        resume_from=config.resume.checkpoint, run_kind=config.run_kind
    )

    # Trainer は best の重みを model へ読み戻してから final.pt を書く。
    # calibration も weights.pt もその重みに対して作る。
    calibration, calibration_reason = _calibrated(task, data, device=trainer.device)
    if calibration is not None:
        calibration.save(run_directory / CALIBRATION_FILE_NAME)
    save_model_weights(
        run_directory / WEIGHTS_FILE_NAME,
        ModelWeights(
            model_family=MODEL_FAMILY,
            model_config=config.model,
            constraints=config.data.constraints,
            dataset_fingerprint=data.dataset_fingerprint,
            model_state=task.model.state_dict(),
        ),
    )
    return outcome, calibration_reason


def main(argv: Sequence[str] | None = None) -> int:
    """Argv から 1 fold を学習する.

    0 は最後まで回りきった run、1 は理由を stderr へ出した失敗、
    :data:`INTERRUPTED_EXIT_CODE` は signal / deadline で打ち切られた run。

    打ち切りを 0 で返すと、5 fold の shell ループ（``set -e``）が次の fold へ
    進み、途中で止まった fold の成果物が report に混ざる。
    """

    arguments = list(sys.argv[1:] if argv is None else argv)
    config, error = compose_experiment(arguments)
    if config is None:
        return _failed(error)
    if error := config.validate():
        return _failed(error)
    logger, error = _experiment_logger(config)
    if logger is None:
        return _failed(error)
    outcome, error = run_training(config, logger=logger)
    if outcome is None:
        return _failed(error)
    print(
        f"stop_reason={outcome.stop_reason} "
        f"epochs={outcome.epochs_completed} "
        f"best_epoch={outcome.best_epoch} "
        f"best_{config.trainer.monitor}={outcome.best_monitor_value} "
        f"run_directory={config.run_directory}"
    )
    if error is not None:
        print(error, file=sys.stderr)
    if outcome.stop_reason not in COMPLETED_STOP_REASONS:
        print(
            f"run が最後まで回りきっていません: stop_reason={outcome.stop_reason}",
            file=sys.stderr,
        )
        return INTERRUPTED_EXIT_CODE
    return 1 if error is not None else 0


def _experiment_logger(
    config: PasteVolumeExperimentConfig,
) -> tuple[ExperimentLogger | None, str | None]:
    """設定から記録先を作る.

    Resume では checkpoint が持つ run_id と同じ run を開く。

    :class:`~ml.training.loop.Trainer` は resume 元と logger の run_id が
    食い違うと拒否するので、新しい run を開くと再開そのものができない。
    """

    if config.logger is None:
        return None, _NO_LOGGER_REASON
    resume_run_id: str | None = None
    if config.resume.checkpoint is not None:
        checkpoint, error = CheckpointStore(config.run_directory).load_path(
            config.resume.checkpoint
        )
        if checkpoint is None:
            return None, error
        resume_run_id = checkpoint.run_id
    return config.logger.build(resume_run_id=resume_run_id), None


_NO_LOGGER_REASON = (
    "logger を選んでいません（logger=mlflow と "
    "logger.tracking_uri=sqlite:////abs/mlflow.db と "
    "logger.artifact_location=/abs/mlartifacts を渡してください）"
)


def _budget_rejection(size: ModelSize) -> str | None:
    """仕様書 §2 の parameter 数・計算量の上限を超えていないかを返す."""

    if size.parameter_count > PARAMETER_BUDGET:
        return (
            f"parameter 数が上限を超えています: {size.parameter_count} > "
            f"{PARAMETER_BUDGET}"
        )
    if size.giga_multiply_accumulate > GIGA_MULTIPLY_ACCUMULATE_BUDGET:
        return (
            "計算量が上限を超えています: "
            f"{size.giga_multiply_accumulate:.6f} GMAC > "
            f"{GIGA_MULTIPLY_ACCUMULATE_BUDGET} GMAC"
            f"（{BUDGET_CROP_SIZE_PX} px x {BUDGET_VIEW_COUNT} view）"
        )
    return None


def _load_initial_weights(model: MultiViewGaussianRegressor, path: Path) -> str | None:
    """Fine-tune の起点になる weight を読み込む.

    キー集合が現在の model と食い違えば理由を返す。

    ``load_state_dict`` の既定は欠けたキーを例外にするが、理由が entrypoint の
    契約（例外ではなく理由文字列）から外れるので先に突き合わせる。
    """

    weights, error = load_model_weights(path)
    if weights is None:
        return error
    expected = set(model.state_dict())
    actual = set(weights.model_state)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        return (
            "initial_weights のキー集合が現在の model と一致しません"
            f"（不足: {missing}、余分: {unknown}）"
        )
    model.load_state_dict(dict(weights.model_state))
    return None


def _calibrated(
    task: PasteVolumeTask,
    data: PasteVolumeTrainingData,
    *,
    device: torch.device,
) -> tuple[UncertaintyCalibration | None, str | None]:
    """学習後の validation split で log 分散 offset を 1 個 fit する.

    平均は変えない（仕様書 §3）。

    test split は触らない。offset を test で合わせると coverage の評価が自己参照になる。
    """

    split: SplitName = "validation"
    collected, error = collect_predictions(task, data, split=split, device=device)
    if collected is None:
        return None, error
    predictions = collected.predictions
    offset, error = predictions.fit_log_variance_offset()
    if offset is None:
        return None, error
    before, before_reason = GaussianRegressionMetrics.measure(predictions)
    if before is None:
        return None, before_reason
    after, after_reason = GaussianRegressionMetrics.measure(
        predictions.with_log_variance_offset(offset)
    )
    if after is None:
        return None, after_reason
    return (
        UncertaintyCalibration(
            split=split,
            sample_count=before.valid_sample_count,
            log_variance_offset=offset,
            coverage_before=before.one_standard_deviation_coverage,
            coverage_after=after.one_standard_deviation_coverage,
        ),
        None,
    )


def _startup_artifacts(
    run_directory: Path, *, provenance: GitProvenance | None
) -> list[Path]:
    """Run の開始と同時に記録先へ載せる file.

    dirty な作業ツリーの diff も残す（仕様書 §3 再現性）。

    commit だけでは、その commit と実際に走ったコードの差が追えない。
    """

    artifacts = [run_directory / CONFIG_FILE_NAME, run_directory / SPLIT_FILE_NAME]
    if provenance is not None and provenance.dirty:
        diff_path = run_directory / GIT_DIFF_FILE_NAME
        atomic_write_text(diff_path, provenance.diff)
        artifacts.append(diff_path)
    return artifacts


def _run_tags(
    config: PasteVolumeExperimentConfig,
    *,
    data: PasteVolumeTrainingData,
    run_directory: Path,
    provenance: GitProvenance | None,
    provenance_reason: str | None,
) -> dict[str, str]:
    """Run の由来と同一性を表すタグ.

    Resume のたびに変わりうる値は param ではなくタグにする。

    param は一度記録すると値を変えられないので、再開そのものが失敗する。
    """

    manifest = data.split_manifest
    tags = {
        "model.family": MODEL_FAMILY,
        "model.schema_version": str(WEIGHTS_SCHEMA_VERSION),
        "dataset.fingerprint": data.dataset_fingerprint,
        "dataset.machine_ids": ",".join(
            sorted({entry.machine_id for entry in data.index.entries})
        ),
        "split.fingerprint": _split_fingerprint(manifest),
        "split.dimension": data.split_dimension,
        "split.held_out_session": config.data.held_out_session or _MISSING_PARAM_VALUE,
        "training.run_directory": str(run_directory),
    }
    if parent := _parent_run_id(config.model.initial_weights):
        tags["model.parent_run_id"] = parent
    if provenance is None:
        tags["git.unavailable"] = provenance_reason or "理由不明"
    else:
        tags.update(provenance.as_tags())
    if deviation := _mean_bias_deviation(config.model, data=data):
        tags["model.mean_bias_deviation"] = deviation
    return tags


def _run_params(
    config: PasteVolumeExperimentConfig,
    *,
    data: PasteVolumeTrainingData,
    size: ModelSize,
    frozen: Sequence[str],
) -> dict[str, Scalar]:
    """Run を通して変わらない設定と実測値.

    ``trainable_parameter_count`` を載せるのは、freeze の掛け忘れを run 記録から
    見える唯一の観測点にするため。

    ``experiment=fine_tune`` を選んでも凍結を忘れた run は全層 fine-tune として
    完走し、metric も成果物も何ひとつ変わらない。

    batch の pixel 予算も載せる（仕様書 §4）。可変 shape の batch sampler では、
    1 batch の sample 数ではなく予算のほうが run を再現する値になる。
    """

    index = data.index
    manifest = data.split_manifest
    params: dict[str, Scalar] = {
        "model.family": MODEL_FAMILY,
        "model.parameter_count": size.parameter_count,
        "model.trainable_parameter_count": size.trainable_parameter_count,
        "model.frozen_parameter_tensor_count": len(frozen),
        "model.giga_multiply_accumulate": size.giga_multiply_accumulate,
        "model.budget_crop_size_px": BUDGET_CROP_SIZE_PX,
        "model.budget_view_count": BUDGET_VIEW_COUNT,
        "data.split_dimension": data.split_dimension,
        "data.held_out_session": config.data.held_out_session or _MISSING_PARAM_VALUE,
        "data.sample_count": len(index.entries),
        "data.rejection_count": len(index.rejections),
        "data.smallest_source_size": index.smallest_source_size,
        "data.train_sample_count": len(manifest.train_sample_ids),
        "data.validation_sample_count": len(manifest.validation_sample_ids),
        "data.test_sample_count": len(manifest.test_sample_ids),
        "data.train_measured_mean_ul": _train_measured_mean(data),
        "data.max_batch_pixels": config.data.max_batch_pixels,
        "data.max_batch_size": config.data.max_batch_size,
        "split.seed": manifest.seed,
    }
    for field in attrs.fields(type(config.model)):
        params[f"model.{field.name}"] = _as_param(getattr(config.model, field.name))
    for field in attrs.fields(type(config.data.constraints)):
        params[f"constraints.{field.name}"] = _as_param(
            getattr(config.data.constraints, field.name)
        )
    for field in attrs.fields(type(config.data.augmentation)):
        params[f"augmentation.{field.name}"] = _as_param(
            getattr(config.data.augmentation, field.name)
        )
    params.update(DependencyVersions.collect().as_params())
    if config.logger is not None:
        params["logger.tracking_uri"] = (
            config.logger.run_target().sanitized_tracking_uri
        )
    return params


def _parent_run_id(initial_weights: Path | None) -> str | None:
    """Fine-tune の起点になった run の識別子.

    ``weights.pt`` は run 識別子を持たないので、同じ run directory に残る
    ``best.pt`` から引く。

    weight の所在（``model.initial_weights`` param）だけでは、その weight を
    作った run の記録へ 1 手で辿れない（仕様書 §4 の parent base run ID）。
    """

    if initial_weights is None:
        return None
    checkpoint, _ = CheckpointStore(initial_weights.parent).load("best")
    return None if checkpoint is None else checkpoint.run_id


def _mean_bias_deviation(
    model: PasteVolumeModelConfig, *, data: PasteVolumeTrainingData
) -> str | None:
    """平均 bias の初期値が train split の真値スケールから外れていれば理由を返す.

    ``mean_bias_initial`` を train split の統計から決めない代わりの観測点
    （計画書 R5）。fold ごとに model config が変わると run 間の比較が読めない。
    """

    measured = _train_measured_mean(data)
    if measured <= 0 or model.mean_bias_initial <= 0:
        return None
    ratio = measured / model.mean_bias_initial
    if 1 / MEAN_BIAS_DEVIATION_FACTOR <= ratio <= MEAN_BIAS_DEVIATION_FACTOR:
        return None
    return (
        f"train split の measured 平均 {measured:.6f} uL は "
        f"mean_bias_initial {model.mean_bias_initial} の {ratio:.2f} 倍です"
    )


def _train_measured_mean(data: PasteVolumeTrainingData) -> float:
    """Train split の真値（blank を除く）の平均 [uL]."""

    values = [
        data.index.entry_for(sample_id).measured_volume_ul
        for sample_id in data.sample_ids_for("train")
        if not data.index.entry_for(sample_id).is_blank
    ]
    return statistics.fmean(values) if values else 0.0


def _split_fingerprint(manifest: SplitManifest) -> str:
    """Split の割り当てそのものから決まる fingerprint."""

    return fingerprint_json(
        {
            "seed": manifest.seed,
            "train": list(manifest.train_sample_ids),
            "validation": list(manifest.validation_sample_ids),
            "test": list(manifest.test_sample_ids),
        }
    )


def _structured[T](payload: object, target: type[T]) -> tuple[T | None, str | None]:
    if not isinstance(payload, dict):
        return None, f"{target.__name__} の payload が写像ではありません: {payload!r}"
    return structure_strictly(
        {str(key): value for key, value in payload.items()},
        target,
        converter=make_strict_converter(),
    )


def _repository_root() -> Path:
    """この source tree を含む repository の root."""

    return Path(__file__).resolve().parents[3]


def _as_param(value: object) -> Scalar:
    if isinstance(value, bool | int | float | str):
        return value
    if value is None:
        return _MISSING_PARAM_VALUE
    return str(value)


def _failed(reason: str | None) -> int:
    print(reason, file=sys.stderr)
    return 1


__all__ = [
    "BUDGET_CROP_SIZE_PX",
    "BUDGET_VIEW_COUNT",
    "CALIBRATION_DOCUMENT",
    "COMPLETED_STOP_REASONS",
    "INTERRUPTED_EXIT_CODE",
    "GIGA_MULTIPLY_ACCUMULATE_BUDGET",
    "PARAMETER_BUDGET",
    "ModelWeights",
    "UncertaintyCalibration",
    "load_model_weights",
    "main",
    "run_training",
    "save_model_weights",
]


if __name__ == "__main__":
    raise SystemExit(main())
