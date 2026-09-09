"""Argv を所有する塗布量推定の評価 entrypoint.

学習と同じ ``key=value`` の argv を受けるが、group 層は積まない。

評価に要るのは「どの run を、どの split で測るか」だけで、experiment preset を
選び直すと run が実際に使った設定と食い違う。読むのは run directory に残った
解決済み config そのものとする。

.. code-block:: shell

    uv run python -m ml.paste_volume.evaluate \
        checkpoint=/abs/runs/loso/s0/best.pt split=validation

    uv run python -m ml.paste_volume.evaluate \
        folds=/abs/runs/loso split=test allow_frozen_test=true \
        output=/abs/runs/loso/report.json

``split=test`` は ``allow_frozen_test=true`` を要求する（仕様書 §7）。

test は fold ごとに 1 度だけ測る集合で、学習の完了処理や Optuna trial から
自動で触ってはならない。

不確かさ calibration は run directory の ``calibration.json`` から読み、
log 分散へ足してから coverage を測る。offset は validation で fit した値で、
test では fit し直さない。
"""

from __future__ import annotations

import json
import statistics
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import attrs
import torch
from cattrs import Converter

from ml.artifact.document import DocumentKind
from ml.config.composition import ConfigComposition
from ml.data.split import SplitName
from ml.evaluation.regression import (
    GaussianRegressionMetrics,
    MeanSaturationDiagnostic,
    ZeroTargetMetrics,
)
from ml.evaluation.slices import CategoricalDimension, DiagnosticReport, DiagnosticSlice
from ml.model.multiview import MultiViewGaussianRegressor
from ml.paste_volume.experiment import (
    CALIBRATION_FILE_NAME,
    CONFIG_FILE_NAME,
    SPLIT_FILE_NAME,
    PasteVolumeExperimentConfig,
    UncertaintyCalibration,
    load_experiment_config,
)
from ml.paste_volume.index import PasteVolumeSampleIndex
from ml.paste_volume.model import MODEL_FAMILY, build_paste_volume_model
from ml.paste_volume.task import (
    PasteVolumeTask,
    PasteVolumeTrainingData,
    collect_predictions,
)
from ml.serialization import make_strict_converter
from ml.training.checkpoint import CheckpointRole, CheckpointStore

EVALUATION_REPORT_DOCUMENT = DocumentKind(
    kind="paste-volume-cross-validation-report", schema_version=1
)

# ``folds=`` から run directory を辿るときに読む checkpoint。
#
# ``best.pt`` は calibration 前の validation NLL が最良の時点で、仕様書 §3 の
# model 選択規則そのもの。``final.pt`` は同じ重みを持つが、deadline を使い切った
# run では書かれないことがある。
#
# ``checkpoint=`` で file を名指ししたときは、その file をそのまま測る。
_DEFAULT_ROLE: CheckpointRole = "best"

# Slice を切る次元。LOSO の test split は 1 session なので slice は 1 本になるが、
# cell 単位 split では test に全 session が入るので session ごとの差が読める。
_SESSION_DIMENSION = "session"


@attrs.frozen
class EvaluationDataOverride:
    """収集 session の所在だけを上書きする.

    run directory の config.json は学習した機体の絶対 path を持つ。

    別の機体で report を作り直すときに、それ以外の設定を触らずに済ませる。
    """

    roots: tuple[Path, ...]
    """空にはできない（:meth:`EvaluationRequest.validate` が拒む）."""


@attrs.frozen
class EvaluationRequest:
    """評価 entrypoint が argv から組み立てる要求."""

    checkpoint: Path | None = None
    """1 run だけを測るときの checkpoint。run directory はその親とみなす."""

    folds: Path | None = None
    """交差検証の run directory を集めた親 directory."""

    split: SplitName = "validation"
    allow_frozen_test: bool = False
    output: Path | None = None
    data: EvaluationDataOverride | None = None

    def validate(self) -> str | None:
        """要求の整合を返す."""

        if (self.checkpoint is None) == (self.folds is None):
            return (
                "checkpoint= か folds= のどちらか一方を指定してください"
                f"（checkpoint={self.checkpoint}、folds={self.folds}）"
            )
        if self.split == "test" and not self.allow_frozen_test:
            return (
                "split=test には allow_frozen_test=true が必要です"
                "（test は fold ごとに 1 度だけ測る集合で、学習の完了処理や "
                "Optuna trial から自動実行しません）"
            )
        if self.data is not None and not self.data.roots:
            return 'data.roots が空です（data.roots=["/abs/..."] を指定してください）'
        return None

    def run_directories(self) -> tuple[tuple[Path, ...] | None, str | None]:
        """測る run directory を並べる.

        ``folds=`` は直下の directory のうち checkpoint を持つものだけを拾う。

        report / mlruns のような兄弟 directory を巻き込まないため。
        """

        if self.checkpoint is not None:
            if not self.checkpoint.is_file():
                return None, f"checkpoint が見つかりません: {self.checkpoint}"
            return (self.checkpoint.parent,), None
        folds = self.folds
        if folds is None or not folds.is_dir():
            return None, f"fold の directory が見つかりません: {folds}"
        directories = tuple(
            child
            for child in sorted(folds.iterdir())
            if child.is_dir() and CheckpointStore(child).exists(_DEFAULT_ROLE)
        )
        if not directories:
            return None, (
                f"{_DEFAULT_ROLE} checkpoint を持つ run directory が "
                f"ありません: {folds}"
            )
        return directories, None


@attrs.frozen
class FoldEvaluation:
    """1 fold ぶんの評価結果."""

    run_name: str
    """Run directory の名前。fold の並びと report の行を対応づける."""

    dataset_fingerprint: str
    """この fold が測った母集団。fold をまたいで一致することを report が要求する."""

    held_out_session: str | None
    """Session 次元で test に固定した session。cell 次元では ``None``."""

    split: SplitName
    sample_count: int
    log_variance_offset: float | None
    metrics: GaussianRegressionMetrics
    zero_target: ZeroTargetMetrics | None
    mean_saturation: MeanSaturationDiagnostic
    session_slices: tuple[DiagnosticSlice, ...]


@attrs.frozen
class CrossValidationReport:
    """全 fold の評価をまとめた成果物."""

    dataset_fingerprint: str
    model_family: str
    split: SplitName
    folds: tuple[FoldEvaluation, ...]

    def aggregate(self) -> Mapping[str, float]:
        """Fold 平均と標準偏差を返す.

        標準偏差は不偏（n-1）で、fold が 1 つのときは 0 とする。

        fold ごとの振れが大きいので、平均だけで判断しないための材料として並べて持つ。
        """

        values: dict[str, float] = {"fold_count": float(len(self.folds))}
        for field in attrs.fields(GaussianRegressionMetrics):
            series = [float(getattr(fold.metrics, field.name)) for fold in self.folds]
            values[f"{field.name}_mean"] = statistics.fmean(series)
            values[f"{field.name}_standard_deviation"] = (
                statistics.stdev(series) if len(series) > 1 else 0.0
            )
        return values

    def save(self, path: Path, *, converter: Converter) -> None:
        """封筒付き JSON として書き出す."""

        EVALUATION_REPORT_DOCUMENT.save(path, self, converter=converter)

    @classmethod
    def load(
        cls, path: Path, *, converter: Converter
    ) -> tuple[CrossValidationReport | None, str | None]:
        """書き出した report を読み戻す."""

        return EVALUATION_REPORT_DOCUMENT.load(path, cls, converter=converter)


def evaluate_fold(
    run_directory: Path,
    *,
    split: SplitName,
    checkpoint: Path | None = None,
    roots: Sequence[Path] | None = None,
    device: torch.device | None = None,
) -> tuple[FoldEvaluation | None, str | None]:
    """1 つの run directory を、その run が使った設定のまま測り直す.

    model は config.json から組み直してから checkpoint の重みを読む。

    checkpoint は model 構成を持たないので、config を経由しないと形が決まらない。

    ``checkpoint`` を渡さなければ ``best.pt`` を測る。渡した file は、role に
    関わらずそれ自身を測る（``latest.pt`` を指したのに best が測られない）。

    split は run directory の split.json を再利用する。

    ここで作り直すと、held-out session が同じでも validation と train の割り当てが
    変わりうる。
    """

    config, error = load_experiment_config(run_directory / CONFIG_FILE_NAME)
    if config is None:
        return None, error
    if roots is not None:
        config = attrs.evolve(
            config, data=attrs.evolve(config.data, roots=tuple(roots))
        )
    task, data, error = _restored_run(
        run_directory, config=config, checkpoint=checkpoint
    )
    if task is None or data is None:
        return None, error
    resolved = device if device is not None else torch.device("cpu")
    task.model.to(resolved)
    collected, error = collect_predictions(task, data, split=split, device=resolved)
    if collected is None:
        return None, error

    offset, error = _log_variance_offset(run_directory)
    if error is not None:
        return None, error
    predictions = (
        collected.predictions
        if offset is None
        else collected.predictions.with_log_variance_offset(offset)
    )
    report, error = DiagnosticReport.build(
        predictions,
        dimensions=[
            CategoricalDimension(
                name=_SESSION_DIMENSION,
                values=_session_values(data, collected.sample_ids),
            )
        ],
    )
    if report is None:
        return None, error
    return (
        FoldEvaluation(
            run_name=run_directory.name,
            dataset_fingerprint=data.dataset_fingerprint,
            held_out_session=_held_out_label(data, config=config),
            split=split,
            sample_count=len(collected.sample_ids),
            log_variance_offset=offset,
            metrics=report.overall,
            zero_target=report.zero_target,
            mean_saturation=report.mean_saturation,
            session_slices=report.slices,
        ),
        None,
    )


def evaluate_request(
    request: EvaluationRequest, *, device: torch.device | None = None
) -> tuple[CrossValidationReport | None, str | None]:
    """要求された run をすべて測り、1 つの report にまとめる.

    dataset fingerprint が fold 間で食い違ったら理由を返す。

    別の母集団で学んだ fold を平均すると、その平均が何の推定なのか決まらない。
    """

    if error := request.validate():
        return None, error
    directories, error = request.run_directories()
    if directories is None:
        return None, error
    roots = None if request.data is None else request.data.roots
    folds: list[FoldEvaluation] = []
    for directory in directories:
        fold, error = evaluate_fold(
            directory,
            split=request.split,
            checkpoint=request.checkpoint,
            roots=roots,
            device=device,
        )
        if fold is None:
            return None, f"{directory.name}: {error}"
        folds.append(fold)
    fingerprints = {fold.dataset_fingerprint for fold in folds}
    if len(fingerprints) != 1:
        return None, (
            f"fold ごとに dataset fingerprint が違います: {sorted(fingerprints)}"
        )
    return (
        CrossValidationReport(
            dataset_fingerprint=folds[0].dataset_fingerprint,
            model_family=MODEL_FAMILY,
            split=request.split,
            folds=tuple(folds),
        ),
        None,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Argv から評価 report を作る。0 は成功、1 は理由を stderr へ出して失敗."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    converter = make_strict_converter()
    request, error = ConfigComposition(overrides=tuple(arguments)).structure(
        EvaluationRequest, converter=converter
    )
    if request is None:
        return _failed(error)
    report, error = evaluate_request(request)
    if report is None:
        return _failed(error)
    if request.output is not None:
        request.output.parent.mkdir(parents=True, exist_ok=True)
        report.save(request.output, converter=converter)
    print(json.dumps(dict(report.aggregate()), ensure_ascii=False, indent=2))
    return 0


def _log_variance_offset(run_directory: Path) -> tuple[float | None, str | None]:
    """Run が fit した log 分散 offset を読む.

    ``calibration.json`` が無い run は offset なしで測る（学習が calibration まで
    行かなかった run も report には載せる）。

    file があるのに読めないときは理由を返す。黙って ``None`` にすると、
    calibration が効いた fold と壊れた fold が report 上で同じ形になる。
    """

    path = run_directory / CALIBRATION_FILE_NAME
    if not path.is_file():
        return None, None
    calibration, error = UncertaintyCalibration.load(path)
    if calibration is None:
        return None, error
    return calibration.log_variance_offset, None


def _restored_run(
    run_directory: Path,
    *,
    config: PasteVolumeExperimentConfig,
    checkpoint: Path | None,
) -> tuple[PasteVolumeTask | None, PasteVolumeTrainingData | None, str | None]:
    """Run directory の config と split から task と data を組み直す."""

    index, error = PasteVolumeSampleIndex.from_roots(
        config.data.roots, constraints=config.data.constraints
    )
    if index is None:
        return None, None, error
    split_path = run_directory / SPLIT_FILE_NAME
    if not split_path.is_file():
        return None, None, f"split manifest が見つかりません: {split_path}"
    data, error = PasteVolumeTrainingData.build(
        index,
        collator=config.data.collator(),
        config=config.data.training_config(),
        split_manifest_path=split_path,
    )
    if data is None:
        return None, None, error
    model, error = build_paste_volume_model(config.model)
    if model is None:
        return None, None, error
    store = CheckpointStore(run_directory)
    loaded, error = (
        store.load(_DEFAULT_ROLE) if checkpoint is None else store.load_path(checkpoint)
    )
    if loaded is None:
        return None, None, error
    if error := _key_set_mismatch(model, loaded.model_state):
        return None, None, error
    model.load_state_dict(dict(loaded.model_state))
    return PasteVolumeTask(model), data, None


def _key_set_mismatch(
    model: MultiViewGaussianRegressor, state: Mapping[str, object]
) -> str | None:
    """Checkpoint の state_dict が現在の model と食い違えば理由を返す.

    ``load_state_dict`` の既定は例外だが、evaluate も entrypoint なので理由文字列で
    返す（学習側の ``model.initial_weights`` と同じ契約）。

    config.json と checkpoint の組が壊れているのは、run directory を混ぜたときに
    起きる。
    """

    expected = set(model.state_dict())
    actual = set(state)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if not missing and not unknown:
        return None
    return (
        "checkpoint のキー集合が config.json の model と一致しません"
        f"（不足: {missing}、余分: {unknown}）"
    )


def _session_values(
    data: PasteVolumeTrainingData, sample_ids: Sequence[str]
) -> tuple[str, ...]:
    """予測の並びに対応する session label を返す."""

    return tuple(
        data.index.entry_for(sample_id).session_label for sample_id in sample_ids
    )


def _held_out_label(
    data: PasteVolumeTrainingData, *, config: PasteVolumeExperimentConfig
) -> str | None:
    """Test に固定した session の label.

    argv には fingerprint の前頭一致でも指定できるので、report には人が読める label へ解決した値を残す。
    """

    if data.split_dimension != "session" or config.data.held_out_session is None:
        return None
    fingerprint, _ = data.index.resolve_session(config.data.held_out_session)
    if fingerprint is None:
        return config.data.held_out_session
    for entry in data.index.entries:
        if entry.session_fingerprint == fingerprint:
            return entry.session_label
    return config.data.held_out_session


def _failed(reason: str | None) -> int:
    print(reason, file=sys.stderr)
    return 1


__all__ = [
    "EVALUATION_REPORT_DOCUMENT",
    "CrossValidationReport",
    "EvaluationDataOverride",
    "EvaluationRequest",
    "FoldEvaluation",
    "evaluate_fold",
    "evaluate_request",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
