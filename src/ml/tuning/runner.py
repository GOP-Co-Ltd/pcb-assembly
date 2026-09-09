"""共有 study へ trial を積む 1 プロセス分のループ.

プロセスは起こさない。

並列化の実体は「複数の OS プロセスが 1 個の RDB study を共有する」ことであり、
プロセスを起こすのは運用者（shell と ``CUDA_VISIBLE_DEVICES``）の仕事だから。

``trial_count`` は残り trial 数へ減算せず、常に積み増す。

同じ study へ何プロセスが合流するかを runner が知らない以上、「あと何 trial
足りないか」はどのプロセスにも判断できない。

実験記録との紐付けは呼び出し側が渡す。

``ml.tuning`` が mlflow を import しないためで、紐付けの欠落は
:meth:`~ml.tuning.study.StudyResults.verify_lineage` が事後に検査する。

trial が 1 つも完走しなかった探索は成功にしない。

``catch=(Exception,)`` は objective の失敗と探索空間の構成ミスを区別できないので、
全滅を成功として返すと誤りが呼び出し側へ伝わらない。
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from functools import partial

import attrs
import optuna

from ml.experiment.logger import Scalar
from ml.tuning.search_space import SearchSpace
from ml.tuning.study import (
    DIRECTIONS,
    Direction,
    StudyIdentity,
    StudyResults,
    StudyStorage,
    TrialRecord,
)

_TAG_PREFIX = "hpo."


class NonFiniteTrialValueError(RuntimeError):
    """Objective が非有限の目的値を返し、その trial を続けられない.

    例外を投げるのは optuna へ「この trial は失敗」と伝える手段であって、検証の
    失敗を例外で返しているのではない。

    非有限値を成果物へ載せると ``canonical_json`` が通らず、以後の
    :meth:`HyperparameterSearch.collect` まで恒久的に読めなくなる。
    """


@attrs.frozen
class TrialAssignment:
    """1 個の trial に割り当てられた設定と、その識別子."""

    study_name: str
    trial_number: int
    overrides: Mapping[str, Scalar]

    @property
    def run_name(self) -> str:
        """実験記録の run 名。trial ごとに衝突しない."""

        return f"{self.study_name}-trial-{self.trial_number}"

    def as_tags(self) -> dict[str, str]:
        """探索のメタデータを tag として返す.

        param ではなく tag にする。

        param は一度記録すると変えられないので、resume で値が変わりうるものを param
        側へ載せると再開そのものが失敗する。
        """

        return {
            f"{_TAG_PREFIX}study_name": self.study_name,
            f"{_TAG_PREFIX}trial_number": str(self.trial_number),
        }

    def as_override_arguments(self) -> tuple[str, ...]:
        """``ConfigComposition.overrides`` へそのまま渡せる token 列を返す.

        値は TOML の literal として書く。

        探索した値と単発 run の値が同じ設定経路を通るため。
        """

        return tuple(
            f"{name}={_as_toml_literal(value)}"
            for name, value in sorted(self.overrides.items())
        )


type TrialObjective = Callable[[TrialAssignment], float]


@attrs.frozen
class HyperparameterSearch:
    """1 個の study へ trial を積む探索."""

    identity: StudyIdentity
    storage: StudyStorage
    search_space: SearchSpace
    direction: Direction = "minimize"
    trial_count: int = 20

    def validate(self) -> str | None:
        """探索を始められる構成かを検証する.

        fingerprint の一致まで見るのは、study 名が探索空間の fingerprint を含むため。

        食い違ったままだと、空間の違う trial が 1 個の study へ合流してしまう。
        """

        if error := self.identity.validate():
            return error
        if error := self.storage.validate():
            return error
        if error := self.search_space.validate():
            return error
        if self.identity.search_space_fingerprint != self.search_space.fingerprint:
            return (
                "identity と search_space の fingerprint が一致しません: "
                f"{self.identity.search_space_fingerprint!r} と "
                f"{self.search_space.fingerprint!r}"
            )
        if self.direction not in DIRECTIONS:
            return (
                f"direction は {list(DIRECTIONS)} のいずれかが必要です: "
                f"{self.direction!r}"
            )
        if self.trial_count < 1:
            return f"trial_count は正の整数が必要です: {self.trial_count}"
        return None

    def run(
        self,
        objective: TrialObjective,
    ) -> tuple[StudyResults | None, str | None]:
        """既存の study へ合流し、``trial_count`` 個の trial を積む.

        ``objective`` が投げた例外はその trial の失敗として記録し、探索は続ける。

        1 個の設定で落ちても、残りの探索まで捨てる理由が無いため。

        既存 study の direction と食い違う場合は trial を積まずに理由を返す。

        ``create_study(load_if_exists=True)`` は既存 study の direction を黙って
        採用するので、照合しないと sampler と成果物の best trial が食い違う。

        完走した trial が 1 つも無い場合も理由を返す。
        """

        if error := self.validate():
            return None, error
        study = optuna.create_study(
            study_name=self.identity.study_name,
            storage=self.storage.uri,
            direction=self.direction,
            load_if_exists=True,
        )
        existing = study.direction.name.lower()
        if existing != self.direction:
            return None, (
                f"既存 study の direction と一致しません: study {existing!r} に対して "
                f"{self.direction!r}（study 名 {self.identity.study_name!r}）"
            )
        study.optimize(
            partial(self._evaluate, objective=objective),
            n_trials=self.trial_count,
            catch=(Exception,),
        )
        results, error = self._results(
            study.get_trials(deepcopy=False), experiment_run_ids=None
        )
        if results is None:
            return None, error
        if results.completed_trial_count == 0:
            return None, (
                f"完走した trial が 1 つもありません（{self.trial_count} 件を要求）: "
                f"{_state_breakdown(results.trials)}"
            )
        return results, None

    def collect(
        self,
        *,
        experiment_run_ids: Mapping[int, str] | None = None,
    ) -> tuple[StudyResults | None, str | None]:
        """Trial を回さず、既存 study の結果だけを読む."""

        if error := self.validate():
            return None, error
        try:
            study = optuna.load_study(
                study_name=self.identity.study_name,
                storage=self.storage.uri,
            )
        except KeyError as error:
            return None, (
                f"study を読めません: {self.identity.study_name!r}"
                f"（{self.storage.redacted_uri}、{error}）"
            )
        return self._results(
            study.get_trials(deepcopy=False),
            experiment_run_ids=experiment_run_ids,
        )

    def _evaluate(
        self,
        trial: optuna.trial.Trial,
        *,
        objective: TrialObjective,
    ) -> float:
        assignment = TrialAssignment(
            study_name=self.identity.study_name,
            trial_number=trial.number,
            overrides=self.search_space.suggest(trial),
        )
        value = objective(assignment)
        if not math.isfinite(value):
            raise NonFiniteTrialValueError(
                f"trial {trial.number} の目的値が非有限です: {value}"
            )
        return value

    def _results(
        self,
        trials: Sequence[optuna.trial.FrozenTrial],
        *,
        experiment_run_ids: Mapping[int, str] | None,
    ) -> tuple[StudyResults | None, str | None]:
        run_ids = experiment_run_ids if experiment_run_ids is not None else {}
        records = [
            TrialRecord(
                number=trial.number,
                state=trial.state.name,
                value=trial.values[0] if trial.values else None,
                parameters=dict(trial.params),
                experiment_run_id=run_ids.get(trial.number),
            )
            for trial in trials
        ]
        return StudyResults.build(
            identity=self.identity,
            storage=self.storage,
            direction=self.direction,
            trials=records,
        )


def _state_breakdown(trials: Sequence[TrialRecord]) -> str:
    """State ごとの件数を並べた診断文字列を作る."""

    counts = Counter(trial.state for trial in trials)
    return ", ".join(f"{state}={count}" for state, count in sorted(counts.items()))


def _as_toml_literal(value: Scalar) -> str:
    """上書き token へ載せる TOML literal を作る."""

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return repr(value)


__all__ = [
    "HyperparameterSearch",
    "NonFiniteTrialValueError",
    "TrialAssignment",
    "TrialObjective",
]
