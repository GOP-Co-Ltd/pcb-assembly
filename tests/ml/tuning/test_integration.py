"""合成 → 探索 → 成果物の通しと、層をまたぐ横断ガード.

計画 §15.6 に対応する。ここだけが守れる契約を置く。

- 同梱 TOML が attrs の既定値を二重に持たないこと
- ``TrainerConfig`` の fingerprint / param / tag の区別が合成経路で崩れないこと
- 探索メタデータが param 側へ漏れないこと
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path

import attrs
import pytest

from ml.config.composition import ConfigComposition
from ml.config.packaged import PackagedConfiguration
from ml.serialization import make_strict_converter
from ml.training.loop import TrainerConfig
from ml.tuning.runner import HyperparameterSearch, TrialAssignment
from ml.tuning.search_space import ParameterDistribution, SearchSpace
from ml.tuning.study import StudyIdentity, StudyResults, StudyStorage

# 同梱 group と、その option が構造化される到達先。
# ``tests/ml/config/test_packaged.py`` の表と同じ対応を持つ。
PACKAGED_GROUP_TARGETS: dict[str, type] = {"trainer": TrainerConfig}

# Fingerprint と param から外れ、tag へ回る時間予算。
#
# その run 限りの予算であって学習の意味論を変えないので、含めると deadline で
# 中断した run を新しい予算で resume できなくなる。
TIME_BUDGET_FIELDS = (
    "deadline_seconds",
    "finalization_grace_seconds",
    "checkpoint_interval_steps",
    "checkpoint_interval_seconds",
)

# ``TrainerConfig`` を構造化するのに最低限必要な、既定値を持たないフィールド。
REQUIRED_TRAINER_LINES = 'max_epochs = 40\nmonitor = "relative_error_score"\n'


def _duplicated_defaults(
    target: type, data: Mapping[str, object], *, prefix: str = ""
) -> list[str]:
    """``data`` のうち、到達先 attrs の既定値と等しい値を書いているキーを返す.

    既定値が factory のフィールドは比較対象にしない（呼び出しに副作用がありうる）。
    """

    fields = {field.name: field for field in attrs.fields(target)}
    duplicated: list[str] = []
    for name, value in data.items():
        field = fields.get(name)
        if field is None:
            # 未知キーは strict converter 側のテストが拒否を守る
            continue
        default = field.default
        # ``attrs.Factory`` は stub 上は overload された関数なので ``isinstance``
        # の第 2 引数にできない。factory 属性の有無で判定する
        if default is attrs.NOTHING or hasattr(default, "factory"):
            continue
        # 入れ子は annotation ではなく既定値の実体から辿る。``loop.py`` は
        # ``from __future__ import annotations`` なので ``field.type`` は文字列になる
        if isinstance(value, Mapping) and attrs.has(type(default)):
            duplicated.extend(
                _duplicated_defaults(type(default), value, prefix=f"{prefix}{name}.")
            )
            continue
        # ``bool`` は ``int`` の部分型なので、値の一致だけでは区別できない
        if value == default and type(value) is type(default):
            duplicated.append(f"{prefix}{name}")
    return duplicated


def _structure_trainer(*layer_paths: Path) -> TrainerConfig:
    value, error = ConfigComposition(layer_paths=layer_paths).structure(
        TrainerConfig, converter=make_strict_converter()
    )
    assert error is None, error
    assert value is not None
    return value


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class TestDefaultsAreNotDuplicated:
    """同梱 TOML は差分だけを書く（既定値は attrs にのみ置く）."""

    def test_no_packaged_option_repeats_an_attrs_default(self):
        located = PackagedConfiguration.locate()

        offenders: list[str] = []
        checked: list[str] = []
        for group, target in PACKAGED_GROUP_TARGETS.items():
            for option in located.option_names(group):
                path = located.root / group / f"{option}.toml"
                data = tomllib.loads(path.read_text(encoding="utf-8"))
                offenders.extend(
                    f"{group}/{option}.toml: {name}"
                    for name in _duplicated_defaults(target, data)
                )
                checked.append(f"{group}={option}")

        # 走査対象が空でも下の assert は通ってしまうため、探索範囲を先に固定する
        assert checked != []
        assert offenders == []

    def test_every_packaged_option_carries_at_least_one_key(self):
        located = PackagedConfiguration.locate()

        for group in PACKAGED_GROUP_TARGETS:
            for option in located.option_names(group):
                path = located.root / group / f"{option}.toml"
                assert tomllib.loads(path.read_text(encoding="utf-8")) != {}


class TestFingerprintClassificationSurvivesComposition:
    """時間予算と学習の意味論の区別が合成経路で崩れない."""

    def test_two_layers_differing_only_in_the_time_budget_share_a_fingerprint(
        self, tmp_path: Path
    ):
        first = _write(
            tmp_path / "first.toml",
            REQUIRED_TRAINER_LINES
            + "deadline_seconds = 3300.0\n"
            + "finalization_grace_seconds = 120.0\n"
            + "checkpoint_interval_steps = 50\n"
            + "checkpoint_interval_seconds = 300.0\n",
        )
        second = _write(
            tmp_path / "second.toml",
            REQUIRED_TRAINER_LINES
            + "deadline_seconds = 900.0\n"
            + "finalization_grace_seconds = 30.0\n"
            + "checkpoint_interval_steps = 20\n"
            + "checkpoint_interval_seconds = 60.0\n",
        )

        assert (
            _structure_trainer(first).fingerprint
            == _structure_trainer(second).fingerprint
        )

    def test_two_layers_differing_in_the_learning_rate_do_not_share_a_fingerprint(
        self, tmp_path: Path
    ):
        first = _write(
            tmp_path / "first.toml",
            REQUIRED_TRAINER_LINES + "learning_rate = 1.0e-4\n",
        )
        second = _write(
            tmp_path / "second.toml",
            REQUIRED_TRAINER_LINES + "learning_rate = 2.0e-4\n",
        )

        assert (
            _structure_trainer(first).fingerprint
            != _structure_trainer(second).fingerprint
        )

    def test_the_time_budget_never_reaches_the_params(self, tmp_path: Path):
        layer = _write(
            tmp_path / "edge.toml",
            REQUIRED_TRAINER_LINES + "deadline_seconds = 3300.0\n",
        )

        params = _structure_trainer(layer).as_params()

        assert set(params).isdisjoint(TIME_BUDGET_FIELDS)

    def test_the_time_budget_reaches_the_tags_with_the_training_prefix(
        self, tmp_path: Path
    ):
        layer = _write(
            tmp_path / "edge.toml",
            REQUIRED_TRAINER_LINES + "deadline_seconds = 3300.0\n",
        )

        tags = _structure_trainer(layer).as_tags()

        assert {f"training.{name}" for name in TIME_BUDGET_FIELDS} <= set(tags)
        assert "3300.0" in tags["training.deadline_seconds"]


class TestSearchedValuesDoNotEnterParams:
    """探索メタデータは tag、探索された設定値は param."""

    @staticmethod
    def _assignment() -> TrialAssignment:
        return TrialAssignment(
            study_name="gaussian-regressor-aaaaaaaaaaaa-bbbbbbbbbbbb",
            trial_number=2,
            overrides={"learning_rate": 3.0e-4},
        )

    def test_search_metadata_never_shares_a_key_with_the_params(self, tmp_path: Path):
        layer = _write(tmp_path / "edge.toml", REQUIRED_TRAINER_LINES)
        params = _structure_trainer(layer).as_params()

        tags = self._assignment().as_tags()

        # param は一度記録すると値を変えられないので、resume のたびに変わる
        # study 名 / trial 番号が param 側へ載ると再開そのものが失敗する
        assert set(tags).isdisjoint(set(params))

    def test_the_searched_value_itself_is_recorded_as_a_param(self, tmp_path: Path):
        layer = _write(tmp_path / "edge.toml", REQUIRED_TRAINER_LINES)
        assignment = self._assignment()

        value, error = ConfigComposition(
            layer_paths=(layer,), overrides=assignment.as_override_arguments()
        ).structure(TrainerConfig, converter=make_strict_converter())

        assert error is None
        assert value is not None
        # trial 内では不変なので param 側に載るのが正しい
        assert value.as_params()["learning_rate"] == 3.0e-4


class TestComposeThenSearch:
    """同梱設定の合成から探索・成果物までの通し."""

    @staticmethod
    def _search(tmp_path: Path, *, trial_count: int = 2) -> HyperparameterSearch:
        # 到達先が ``TrainerConfig`` なので、上書きキーは節を前置きしない
        search_space = SearchSpace(
            parameters={
                "learning_rate": ParameterDistribution(
                    kind="float", low=1.0e-5, high=1.0e-2, log=True
                )
            }
        )
        identity, error = StudyIdentity.build(
            model_family="gaussian-regressor",
            dataset_fingerprint=f"sha256:{'a' * 64}",
            # 探索空間と整合させる（裁定 2）
            search_space_fingerprint=search_space.fingerprint,
        )
        assert error is None
        assert identity is not None
        return HyperparameterSearch(
            identity=identity,
            storage=StudyStorage(uri=f"sqlite:///{tmp_path / 'hpo.db'}"),
            search_space=search_space,
            trial_count=trial_count,
        )

    def test_runs_a_search_over_the_packaged_configuration(self, tmp_path: Path):
        located = PackagedConfiguration.locate()
        converter = make_strict_converter()
        structured: list[TrainerConfig] = []

        def objective(assignment: TrialAssignment) -> float:
            composition, error = ConfigComposition.from_arguments(
                ("trainer=edge", *assignment.as_override_arguments()),
                configuration_root=located.root,
            )
            assert error is None, error
            assert composition is not None
            config, structure_error = composition.structure(
                TrainerConfig, converter=converter
            )
            assert structure_error is None, structure_error
            assert config is not None
            structured.append(config)
            return config.learning_rate

        results, error = self._search(tmp_path).run(objective)

        assert error is None
        assert results is not None
        assert len(structured) == 2
        assert results.best_trial is not None

    def test_saves_a_document_whose_lineage_verifies(self, tmp_path: Path):
        located = PackagedConfiguration.locate()
        converter = make_strict_converter()
        search = self._search(tmp_path)

        def objective(assignment: TrialAssignment) -> float:
            composition, error = ConfigComposition.from_arguments(
                ("trainer=edge", *assignment.as_override_arguments()),
                configuration_root=located.root,
            )
            assert error is None, error
            assert composition is not None
            config, structure_error = composition.structure(
                TrainerConfig, converter=converter
            )
            assert structure_error is None, structure_error
            assert config is not None
            return config.learning_rate

        _, run_error = search.run(objective)
        assert run_error is None
        collected, collect_error = search.collect(
            experiment_run_ids={0: "run-a", 1: "run-b"}
        )
        assert collect_error is None
        assert collected is not None
        target = tmp_path / "hyperparameter_search_results.json"
        collected.save(target, converter=converter)

        loaded, load_error = StudyResults.load(target, converter=converter)

        assert load_error is None
        assert loaded is not None
        assert loaded.verify_lineage() is None
        assert loaded == collected


class TestRegisteredGroups:
    """上の表と同梱実体の食い違いを防ぐ."""

    @pytest.mark.parametrize("group", sorted(PACKAGED_GROUP_TARGETS))
    def test_every_registered_group_is_packaged(self, group: str):
        assert group in PackagedConfiguration.locate().group_names()
