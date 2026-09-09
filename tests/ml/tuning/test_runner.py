"""共有 study へ trial を積む runner の公開契約.

計画 §13.5 / §15.5 に対応する。optuna と SQLite は実物を使い、storage を ``tmp_path``
に置く。並列 run の合流は「同一 storage を指す 2 インスタンス」で 検証する。SQLite
ファイル経由の合流経路はプロセス境界と同一（計画 §11）。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path

import pytest

from ml.config.composition import ConfigComposition
from ml.experiment.logger import Scalar
from ml.tuning.runner import HyperparameterSearch, TrialAssignment
from ml.tuning.search_space import ParameterDistribution, SearchSpace
from ml.tuning.study import Direction, StudyIdentity, StudyStorage

LEARNING_RATE = "trainer.learning_rate"
MAX_EPOCHS = "trainer.max_epochs"


def _identity(
    *,
    model_family: str = "gaussian-regressor",
    search_space: SearchSpace | None = None,
) -> StudyIdentity:
    """探索空間と整合した study 同一性を作る.

    ``search_space_fingerprint`` は探索空間そのものから採る（裁定 2）。
    """

    space = search_space if search_space is not None else _search_space()
    identity, error = StudyIdentity.build(
        model_family=model_family,
        dataset_fingerprint=f"sha256:{'a' * 64}",
        search_space_fingerprint=space.fingerprint,
    )
    assert error is None
    assert identity is not None
    return identity


def _storage(tmp_path: Path) -> StudyStorage:
    return StudyStorage(uri=f"sqlite:///{tmp_path / 'hpo.db'}")


def _search_space() -> SearchSpace:
    return SearchSpace(
        parameters={
            LEARNING_RATE: ParameterDistribution(
                kind="float", low=1.0e-5, high=1.0e-2, log=True
            ),
            MAX_EPOCHS: ParameterDistribution(
                kind="integer", low=2.0, high=10.0, step=2
            ),
        }
    )


def _search(
    tmp_path: Path,
    *,
    trial_count: int = 2,
    direction: Direction = "minimize",
    identity: StudyIdentity | None = None,
    search_space: SearchSpace | None = None,
    storage: StudyStorage | None = None,
) -> HyperparameterSearch:
    space = search_space if search_space is not None else _search_space()
    return HyperparameterSearch(
        identity=identity or _identity(search_space=space),
        storage=storage or _storage(tmp_path),
        search_space=space,
        direction=direction,
        trial_count=trial_count,
    )


class _RecordingObjective:
    """受け取った割り当てを記録し、trial 番号を目的値として返す objective.

    ``failing_trial_numbers`` に挙げた trial だけ例外を投げ、
    ``non_finite_trial_numbers`` に挙げた trial だけ ``inf`` を返す。
    """

    def __init__(
        self,
        *,
        failing_trial_numbers: frozenset[int] = frozenset(),
        non_finite_trial_numbers: frozenset[int] = frozenset(),
    ) -> None:
        self.assignments: list[TrialAssignment] = []
        self._failing_trial_numbers = failing_trial_numbers
        self._non_finite_trial_numbers = non_finite_trial_numbers

    def __call__(self, assignment: TrialAssignment) -> float:
        self.assignments.append(assignment)
        if assignment.trial_number in self._failing_trial_numbers:
            raise RuntimeError("この trial は失敗する")
        if assignment.trial_number in self._non_finite_trial_numbers:
            return float("inf")
        return float(assignment.trial_number)

    @property
    def call_count(self) -> int:
        """呼び出された回数."""

        return len(self.assignments)


class TestTrialAssignment:
    """1 trial 分の割り当てが持つ名前と上書き."""

    @staticmethod
    def _assignment(
        *, trial_number: int = 3, overrides: Mapping[str, Scalar] | None = None
    ) -> TrialAssignment:
        return TrialAssignment(
            study_name="gaussian-regressor-aaaaaaaaaaaa-bbbbbbbbbbbb",
            trial_number=trial_number,
            overrides=overrides if overrides is not None else {LEARNING_RATE: 1.0e-4},
        )

    def test_names_the_run_after_the_study_and_the_trial_number(self):
        assignment = self._assignment()

        assert assignment.run_name == (
            "gaussian-regressor-aaaaaaaaaaaa-bbbbbbbbbbbb-trial-3"
        )

    def test_prefixes_every_tag_with_the_search_namespace(self):
        tags = self._assignment().as_tags()

        assert set(tags) == {"hpo.study_name", "hpo.trial_number"}
        assert tags["hpo.trial_number"] == "3"

    def test_produces_override_arguments_that_the_composition_accepts(self):
        assignment = self._assignment(overrides={LEARNING_RATE: 1.0e-4, MAX_EPOCHS: 6})

        arguments = assignment.as_override_arguments()

        data, error = ConfigComposition(overrides=arguments).compose()
        assert error is None
        assert data == {"trainer": {"learning_rate": 1.0e-4, "max_epochs": 6}}


class TestHyperparameterSearchValidation:
    """実行前の整合検証."""

    def test_reports_a_non_positive_trial_count(self, tmp_path: Path):
        assert _search(tmp_path, trial_count=0).validate() is not None

    def test_reports_storage_that_cannot_be_shared(self, tmp_path: Path):
        search = _search(tmp_path, storage=StudyStorage(uri="sqlite:///relative.db"))

        assert search.validate() is not None

    def test_reports_an_empty_search_space(self, tmp_path: Path):
        search = _search(tmp_path, search_space=SearchSpace(parameters={}))

        assert search.validate() is not None

    def test_reports_a_search_space_that_does_not_match_the_identity(
        self, tmp_path: Path
    ):
        """探索空間の fingerprint が study 同一性と食い違うのを拒否する（裁定 2）.

        違う探索空間の trial が 1 つの study へ合流すると、best_trial の意味が壊れる。
        """

        other_space = SearchSpace(
            parameters={
                LEARNING_RATE: ParameterDistribution(
                    kind="float", low=1.0e-4, high=1.0e-1
                )
            }
        )

        mismatched = _search(tmp_path, identity=_identity(), search_space=other_space)

        assert _search(tmp_path).validate() is None
        assert mismatched.validate() is not None

    def test_reports_a_search_space_that_optuna_cannot_sample(self, tmp_path: Path):
        """``suggest`` が必ず投げる探索空間を、study を作る前に拒否する."""

        unsamplable = SearchSpace(
            parameters={
                MAX_EPOCHS: ParameterDistribution(
                    kind="integer", low=1.0, high=100.0, log=True, step=2
                )
            }
        )

        search = _search(tmp_path, search_space=unsamplable)

        assert search.validate() is not None

    def test_accepts_a_consistent_search(self, tmp_path: Path):
        assert _search(tmp_path).validate() is None


class TestHyperparameterSearchRun:
    """共有 study への trial の積み上げ."""

    def test_runs_the_requested_number_of_trials(self, tmp_path: Path):
        objective = _RecordingObjective()

        results, error = _search(tmp_path, trial_count=3).run(objective)

        assert error is None
        assert results is not None
        assert objective.call_count == 3
        assert len(results.trials) == 3

    def test_numbers_the_trials_from_zero(self, tmp_path: Path):
        objective = _RecordingObjective()

        _, error = _search(tmp_path, trial_count=3).run(objective)

        assert error is None
        assert [assignment.trial_number for assignment in objective.assignments] == [
            0,
            1,
            2,
        ]

    def test_assigns_every_searched_parameter(self, tmp_path: Path):
        objective = _RecordingObjective()

        _, error = _search(tmp_path, trial_count=2).run(objective)

        assert error is None
        for assignment in objective.assignments:
            assert set(assignment.overrides) == set(_search_space().parameters)

    def test_names_the_results_after_the_identity(self, tmp_path: Path):
        search = _search(tmp_path)

        results, error = search.run(_RecordingObjective())

        assert error is None
        assert results is not None
        assert results.study_name == search.identity.study_name

    def test_never_records_the_raw_storage_uri(self, tmp_path: Path):
        search = _search(tmp_path)

        results, error = search.run(_RecordingObjective())

        assert error is None
        assert results is not None
        assert results.storage_uri_redacted == search.storage.redacted_uri

    def test_joins_trials_from_two_instances_sharing_one_storage(self, tmp_path: Path):
        """並列 run の合流。計画 §11 の要件 1.

        同じ storage ファイルを指す 2 インスタンスの trial が 1 つの study へ集まる。

        ``load_if_exists`` が外れると 2 つ目が別 study を作るか例外になる。
        """

        storage = _storage(tmp_path)
        first = _search(tmp_path, trial_count=2, storage=storage)
        second = _search(tmp_path, trial_count=3, storage=storage)

        first_results, first_error = first.run(_RecordingObjective())
        second_results, second_error = second.run(_RecordingObjective())

        assert first_error is None
        assert second_error is None
        assert first_results is not None
        assert second_results is not None
        assert len(first_results.trials) == 2
        assert len(second_results.trials) == 5
        assert second_results.study_name == first_results.study_name

    def test_resumes_an_existing_study_without_reducing_the_trial_count(
        self, tmp_path: Path
    ):
        """Resume。計画 §11 の要件 2・3.

        3 つ目のインスタンスが既存 study を読み、``trial_count`` を残 trial 数へ 減算せずに積み増す。

        MR185 の ``n_trials`` 減算ロジックが復活すると trial 総数が足りなくなる。
        """

        storage = _storage(tmp_path)
        for trial_count in (2, 3):
            _, error = _search(tmp_path, trial_count=trial_count, storage=storage).run(
                _RecordingObjective()
            )
            assert error is None

        third = _RecordingObjective()
        results, error = _search(tmp_path, trial_count=4, storage=storage).run(third)

        assert error is None
        assert results is not None
        assert third.call_count == 4
        assert len(results.trials) == 9
        numbers = [trial.number for trial in results.trials]
        assert sorted(numbers) == list(range(9))

    def test_separates_the_run_name_of_every_trial(self, tmp_path: Path):
        """Trial ごとに独立した割り当てが渡る。計画 §11 の要件 3."""

        objective = _RecordingObjective()

        _, error = _search(tmp_path, trial_count=3).run(objective)

        assert error is None
        run_names = {assignment.run_name for assignment in objective.assignments}
        assert len(run_names) == 3

    @pytest.mark.parametrize(
        "search_argument",
        ["trial_count", "storage", "search_space"],
    )
    def test_does_not_create_a_study_when_validation_fails(
        self, tmp_path: Path, search_argument: str
    ):
        database = tmp_path / "hpo.db"
        storage = StudyStorage(uri=f"sqlite:///{database}")
        invalid = {
            "trial_count": _search(tmp_path, trial_count=0, storage=storage),
            "storage": _search(
                tmp_path, storage=StudyStorage(uri="sqlite:///relative.db")
            ),
            "search_space": _search(
                tmp_path, search_space=SearchSpace(parameters={}), storage=storage
            ),
        }[search_argument]
        objective = _RecordingObjective()

        results, error = invalid.run(objective)

        assert results is None
        assert error is not None
        assert objective.call_count == 0
        assert not database.exists()

    def test_reports_an_existing_study_with_a_different_direction(self, tmp_path: Path):
        """既存 study と direction が食い違ったら trial を積まずに拒否する.

        ``create_study(load_if_exists=True)`` は既存の direction を黙って採用するので、
        照合しないと sampler と成果物の best trial が食い違う。
        """

        storage = _storage(tmp_path)
        _, error = _search(
            tmp_path, trial_count=2, storage=storage, direction="minimize"
        ).run(_RecordingObjective())
        assert error is None

        objective = _RecordingObjective()
        results, mismatch = _search(
            tmp_path, trial_count=2, storage=storage, direction="maximize"
        ).run(objective)

        assert results is None
        assert mismatch is not None
        assert "minimize" in mismatch
        assert objective.call_count == 0
        # 拒否した側の trial は 1 つも積まれていない
        collected, collect_error = _search(
            tmp_path, storage=storage, direction="minimize"
        ).collect()
        assert collect_error is None
        assert collected is not None
        assert len(collected.trials) == 2

    def test_reports_a_search_where_no_trial_completed(self, tmp_path: Path):
        """全 trial が落ちたら成功を返さない.

        ``catch=(Exception,)`` は objective の失敗と探索空間の構成ミスを区別できない。
        """

        objective = _RecordingObjective(failing_trial_numbers=frozenset({0, 1, 2}))

        results, error = _search(tmp_path, trial_count=3).run(objective)

        assert results is None
        assert error is not None
        # state の内訳と要求 trial 数を別々に観測する。``assert "3" in error`` だと
        # 先行する "FAIL=3" で満たされ、要求数を落とす変異が生き残る
        assert "FAIL=3" in error
        assert "3 件を要求" in error

    def test_records_a_non_finite_objective_value_as_a_failure(self, tmp_path: Path):
        """非有限の目的値はその trial の失敗として扱う（MR4 と同じ扱い）.

        成果物へ ``inf`` を載せると ``canonical_json`` が通らず、以後の ``collect``
        まで恒久的に読めなくなる。
        """

        objective = _RecordingObjective(non_finite_trial_numbers=frozenset({1}))

        results, error = _search(tmp_path, trial_count=3).run(objective)

        assert error is None
        assert results is not None
        assert results.completed_trial_count == 2
        assert [trial.state for trial in results.trials].count("FAIL") == 1
        assert all(
            trial.value is None or math.isfinite(trial.value)
            for trial in results.trials
        )
        # 読み直しても成果物が壊れていない
        collected, collect_error = _search(tmp_path).collect()
        assert collect_error is None
        assert collected is not None

    def test_records_a_failing_trial_without_failing_the_whole_search(
        self, tmp_path: Path
    ):
        objective = _RecordingObjective(failing_trial_numbers=frozenset({0}))

        results, error = _search(tmp_path, trial_count=3).run(objective)

        assert error is None
        assert results is not None
        assert objective.call_count == 3
        assert "FAIL" in {trial.state for trial in results.trials}
        assert results.completed_trial_count == 2


class TestCollect:
    """Trial を回さずに既存 study を読む."""

    def test_reads_an_existing_study_without_running_trials(self, tmp_path: Path):
        storage = _storage(tmp_path)
        search = _search(tmp_path, trial_count=2, storage=storage)
        _, error = search.run(_RecordingObjective())
        assert error is None

        results, collect_error = search.collect()

        assert collect_error is None
        assert results is not None
        assert len(results.trials) == 2

    def test_fills_the_experiment_run_ids_that_are_given(self, tmp_path: Path):
        search = _search(tmp_path, trial_count=2)
        _, error = search.run(_RecordingObjective())
        assert error is None

        results, collect_error = search.collect(
            experiment_run_ids={0: "run-a", 1: "run-b"}
        )

        assert collect_error is None
        assert results is not None
        assert {trial.number: trial.experiment_run_id for trial in results.trials} == {
            0: "run-a",
            1: "run-b",
        }
        assert results.verify_lineage() is None

    def test_leaves_the_experiment_run_ids_empty_by_default(self, tmp_path: Path):
        search = _search(tmp_path, trial_count=1)
        _, error = search.run(_RecordingObjective())
        assert error is None

        results, collect_error = search.collect()

        assert collect_error is None
        assert results is not None
        assert [trial.experiment_run_id for trial in results.trials] == [None]

    def test_reports_a_study_that_does_not_exist(self, tmp_path: Path):
        storage = _storage(tmp_path)
        _, error = _search(tmp_path, trial_count=1, storage=storage).run(
            _RecordingObjective()
        )
        assert error is None
        absent = _search(
            tmp_path, identity=_identity(model_family="other-family"), storage=storage
        )

        results, collect_error = absent.collect()

        assert results is None
        assert collect_error is not None
