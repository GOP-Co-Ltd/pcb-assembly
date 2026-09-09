"""Argv を所有するハイパーパラメータ探索 entrypoint.

探索した値は ``ConfigComposition`` の ``key=value`` 上書きへ素通しする。

探索 run と単発 run が同じ設定経路を通るので、良かった trial の設定をそのまま
``train`` の argv として打ち直せる。

.. code-block:: shell

    uv run python -m ml.paste_volume.search \
        experiment=search trainer=gpu logger=mlflow \
        hyperparameter_search=base_optuna \
        data.roots='["/abs/data/paste-volume-datasets"]' \
        data.held_out_session="$S" \
        hyperparameter_search.storage_uri="sqlite:////abs/optuna.db" \
        logger.tracking_uri="sqlite:////abs/mlflow.db" \
        logger.artifact_location="/abs/mlartifacts" \
        run_directory="/abs/runs/hpo/$S"

並列化の実体は「複数の OS プロセスが 1 個の storage を共有する」こと。

同じ argv でもう 1 プロセス起こすと、同じ study 名（model family と dataset と
探索空間の fingerprint から決まる）へ合流して trial を積み増す。

``trial_count`` は残 trial 数へ減算せず常に積み増すので、何プロセスが合流するかを
この module は知らない。

Trial ごとに ``run_directory`` を分ける。

1 個の directory を共有すると、split.json も checkpoint も trial 間で上書きし合う。
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import override

import torch

from ml.experiment.logger import ExperimentLogger, TaggedExperimentLogger
from ml.paste_volume.experiment import (
    PasteVolumeExperimentConfig,
    compose_experiment,
)
from ml.paste_volume.index import PasteVolumeSampleIndex
from ml.paste_volume.model import MODEL_FAMILY
from ml.paste_volume.train import run_training
from ml.serialization import make_strict_converter
from ml.tuning.runner import HyperparameterSearch, TrialAssignment
from ml.tuning.study import StudyIdentity, StudyResults


class _TrialLogger(TaggedExperimentLogger):
    """Trial のタグを被せ、start が返した run 識別子を憶えておく logger.

    :meth:`~ml.experiment.logger.ExperimentLogger.run_id` は run を end した
    あとに読めない実装がある（MLflow adapter は :class:`RuntimeError`）。

    trial と実験 run の対応は run が終わってから成果物へ載せるので、start の
    戻り値をここで取っておく。
    """

    def __init__(self, inner: ExperimentLogger, *, tags: Mapping[str, str]) -> None:
        super().__init__(inner, tags=tags)
        self.started_run_id: str | None = None

    @override
    def start(
        self,
        *,
        run_kind: str,
        run_name: str | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> str:
        """Run を開始し、その識別子を憶えてから返す."""

        self.started_run_id = super().start(
            run_kind=run_kind, run_name=run_name, tags=tags
        )
        return self.started_run_id


def run_search(
    arguments: Sequence[str], *, device: torch.device | None = None
) -> tuple[StudyResults | None, str | None]:
    """1 プロセス分の trial を共有 study へ積む.

    ``arguments`` は ``train`` と同じ argv。

    trial ごとに ``TrialAssignment.as_override_arguments()`` を末尾へ足して
    合成し直すので、探索が触るのは argv の 1 層だけになる。
    """

    config, error = compose_experiment(list(arguments))
    if config is None:
        return None, error
    if error := config.validate():
        return None, error
    search_config = config.hyperparameter_search
    if search_config is None:
        return None, (
            "hyperparameter_search を選んでいません"
            "（hyperparameter_search=base_optuna と "
            "hyperparameter_search.storage_uri=sqlite:////abs/optuna.db を"
            "渡してください）"
        )
    if config.logger is None:
        return None, _NO_LOGGER_REASON
    index, error = PasteVolumeSampleIndex.from_roots(
        config.data.roots, constraints=config.data.constraints
    )
    if index is None:
        return None, error
    identity, error = StudyIdentity.build(
        model_family=MODEL_FAMILY,
        dataset_fingerprint=index.dataset_fingerprint,
        search_space_fingerprint=search_config.search_space.fingerprint,
    )
    if identity is None:
        return None, error
    search = HyperparameterSearch(
        identity=identity,
        storage=search_config.storage(),
        search_space=search_config.search_space,
        direction=search_config.direction,
        trial_count=search_config.trial_count,
    )
    run_ids: dict[int, str] = {}

    def objective(assignment: TrialAssignment) -> float:
        return _trial_value(
            assignment,
            arguments=arguments,
            base=config,
            device=device,
            run_ids=run_ids,
        )

    results, error = search.run(objective)
    if results is None:
        return None, error
    # run() は trial と実験 run の対応を知らない（``ml.tuning`` は mlflow を
    # import しない）。積み終えてから紐付けを添えて読み直す。
    results, error = search.collect(experiment_run_ids=run_ids)
    if results is None:
        return None, error
    # 紐付けの検査は成果物を書くより前に置く。あとに置くと、trial と実験 run が
    # 繋がっていない study.json が残ったまま失敗する。
    if reason := results.verify_lineage():
        return None, reason
    if search_config.results_path is not None:
        search_config.results_path.parent.mkdir(parents=True, exist_ok=True)
        results.save(search_config.results_path, converter=make_strict_converter())
    return results, None


def main(argv: Sequence[str] | None = None) -> int:
    """Argv から探索を回す。0 は成功、1 は理由を stderr へ出して失敗."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    results, error = run_search(arguments)
    if results is None:
        print(error, file=sys.stderr)
        return 1
    best = results.best_trial
    print(
        f"study={results.study_name} "
        f"completed={results.completed_trial_count} "
        f"best_trial={None if best is None else best.number} "
        f"best_value={None if best is None else best.value}"
    )
    return 0


_NO_LOGGER_REASON = (
    "logger を選んでいません（logger=mlflow と "
    "logger.tracking_uri=sqlite:////abs/mlflow.db と "
    "logger.artifact_location=/abs/mlartifacts を渡してください）"
)


def _trial_value(
    assignment: TrialAssignment,
    *,
    arguments: Sequence[str],
    base: PasteVolumeExperimentConfig,
    device: torch.device | None,
    run_ids: dict[int, str],
) -> float:
    """1 個の trial を学習し、監視している validation metric の最良値を返す.

    失敗は例外で返す。

    :meth:`~ml.tuning.runner.HyperparameterSearch.run` は objective の例外を
    その trial の失敗として記録し、残りの探索を続ける。理由文字列で返すと
    trial が「成功して非有限値を出した」ことになってしまう。
    """

    trial_arguments = [
        *arguments,
        *assignment.as_override_arguments(),
        f"run_directory={_trial_directory(base.run_directory, assignment)}",
    ]
    config, error = compose_experiment(trial_arguments)
    if config is None:
        raise ValueError(error)
    if error := config.validate():
        raise ValueError(error)
    if config.logger is None:
        raise ValueError(_NO_LOGGER_REASON)
    logger = _TrialLogger(
        config.logger.build(run_name=assignment.run_name),
        tags=assignment.as_tags(),
    )
    outcome, error = run_training(config, logger=logger, device=device)
    if logger.started_run_id is not None:
        run_ids[assignment.trial_number] = logger.started_run_id
    if outcome is None:
        raise ValueError(error)
    if error is not None:
        # 学習は終わったが成果物を 1 つ作れなかった（いまは calibration だけ）。
        # trial 自体は monitor の値を出しているので失敗にはしないが、黙って
        # 捨てない。
        print(f"trial {assignment.trial_number}: {error}", file=sys.stderr)
    if outcome.best_monitor_value is None:
        raise ValueError(
            f"trial {assignment.trial_number} は monitor "
            f"{config.trainer.monitor!r} の値を 1 度も出しませんでした"
            f"（stop_reason={outcome.stop_reason}）"
        )
    return outcome.best_monitor_value


def _trial_directory(base: Path, assignment: TrialAssignment) -> Path:
    """Trial ごとの run directory.

    study 名は model family と 2 つの fingerprint から決まるので、path に
    使える文字だけで構成される。
    """

    return base / assignment.run_name


__all__ = [
    "main",
    "run_search",
]


if __name__ == "__main__":
    raise SystemExit(main())
