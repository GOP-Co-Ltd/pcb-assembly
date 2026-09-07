"""探索空間の宣言と optuna への振り分けの公開契約.

計画 §13.4 / §15.4 に対応する。optuna は 3rd-party だが、in-memory study から 実物の
``Trial`` を取り出して使う（モックを書かない）。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

import attrs
import optuna
import pytest

from ml.config.composition import ConfigComposition
from ml.serialization import make_strict_converter
from ml.tuning.search_space import (
    DistributionKind,
    ParameterDistribution,
    SearchSpace,
)

LEARNING_RATE = "trainer.learning_rate"
MAX_EPOCHS = "trainer.max_epochs"


@attrs.frozen
class _Application:
    """探索空間を節として持つ設定."""

    search_space: SearchSpace


def _trial() -> optuna.trial.Trial:
    """実物の optuna trial を 1 つ取り出す."""

    return optuna.create_study().ask()


class TestParameterDistributionValidation:
    """Kind ごとに必要なフィールドが揃っているかの検証."""

    @pytest.mark.parametrize(
        "distribution",
        [
            ParameterDistribution(kind="float", high=1.0e-2),
            ParameterDistribution(kind="float", low=1.0e-5),
            ParameterDistribution(kind="float", low=1.0e-2, high=1.0e-5),
            ParameterDistribution(kind="float", low=1.0e-2, high=1.0e-2),
            # 有限でない境界。拒否しないと fingerprint が JSON へ書けなくなる
            ParameterDistribution(kind="float", low=1.0e-5, high=float("inf")),
            ParameterDistribution(kind="float", low=float("nan"), high=1.0e-2),
            ParameterDistribution(kind="float", low=0.0, high=1.0e-2, log=True),
            ParameterDistribution(
                kind="float", low=1.0e-5, high=1.0e-2, choices=("a",)
            ),
            # float に step は書けない（``step < 1`` とは別の分岐）
            ParameterDistribution(kind="float", low=1.0e-5, high=1.0e-2, step=2),
            ParameterDistribution(kind="integer", low=2.0, high=10.0, step=0),
            ParameterDistribution(kind="integer", low=2.0, high=10.0, step=-1),
            ParameterDistribution(kind="integer", high=10),
            # 非整数の境界を ``int()`` で黙って切り捨てさせない（裁定 4）
            ParameterDistribution(kind="integer", low=0.5, high=10.0),
            ParameterDistribution(kind="integer", low=2.0, high=10.5),
            ParameterDistribution(kind="categorical"),
            ParameterDistribution(kind="categorical", choices=("a",), low=1.0),
            ParameterDistribution(kind="categorical", choices=("a",), high=1.0),
            ParameterDistribution(kind="categorical", choices=("a",), log=True),
            ParameterDistribution(kind="categorical", choices=("a",), step=2),
            # optuna が常に拒否する組み合わせ（log スケール + step != 1）
            ParameterDistribution(
                kind="integer", low=1.0, high=100.0, log=True, step=2
            ),
            # 境界を int で書いた形。fingerprint が float 版と食い違う
            ParameterDistribution(kind="integer", low=2, high=10.0),
            ParameterDistribution(kind="float", low=1.0e-5, high=1),
            # kind そのものが未知。他の分岐がすべて通る値で書かないと、
            # 許可リスト判定を落としても low / high 必須判定が拾ってしまう
            ParameterDistribution(
                kind=cast("DistributionKind", "unknown"), low=1.0, high=2.0
            ),
        ],
    )
    def test_reports_an_inconsistent_distribution(
        self, distribution: ParameterDistribution
    ):
        assert distribution.validate() is not None

    def test_reports_a_logarithmic_integer_with_a_step(self):
        """``validate`` で optuna が常に拒否する組み合わせを塞ぐ."""

        # suggest まで通すと ``catch=(Exception,)`` に飲まれ、全 trial が黙って失敗する
        error = ParameterDistribution(
            kind="integer", low=1.0, high=100.0, log=True, step=2
        ).validate()

        assert error is not None
        assert "step" in error

    def test_reports_a_bound_written_as_an_integer(self):
        """``low=2`` を拒否する（``2`` と ``2.0`` で fingerprint が変わる）."""

        error = ParameterDistribution(kind="integer", low=2, high=10.0).validate()

        assert error is not None
        assert "low" in error
        assert "float" in error

    @pytest.mark.parametrize(
        "distribution",
        [
            ParameterDistribution(kind="float", low=1.0e-5, high=1.0e-2),
            ParameterDistribution(kind="float", low=1.0e-5, high=1.0e-2, log=True),
            ParameterDistribution(kind="integer", low=2.0, high=10.0),
            ParameterDistribution(kind="integer", low=2.0, high=10.0, step=2),
            ParameterDistribution(
                kind="integer", low=1.0, high=100.0, log=True, step=1
            ),
            ParameterDistribution(kind="categorical", choices=("min", "max")),
        ],
    )
    def test_accepts_a_consistent_distribution(
        self, distribution: ParameterDistribution
    ):
        assert distribution.validate() is None


class TestSuggest:
    """実物の optuna trial への振り分け."""

    def test_suggests_a_float_inside_the_range(self):
        distribution = ParameterDistribution(kind="float", low=1.0e-5, high=1.0e-2)

        value = distribution.suggest(_trial(), name=LEARNING_RATE)

        assert isinstance(value, float)
        assert 1.0e-5 <= value <= 1.0e-2

    def test_suggests_a_logarithmic_float_inside_the_range(self):
        distribution = ParameterDistribution(
            kind="float", low=1.0e-6, high=1.0e-1, log=True
        )

        value = distribution.suggest(_trial(), name=LEARNING_RATE)

        assert isinstance(value, float)
        assert 1.0e-6 <= value <= 1.0e-1

    def test_suggests_an_integer_on_the_step_grid(self):
        """``step`` を optuna の分布そのものへ渡す.

        引いた値が格子上かを見るだけでは不十分。

        ``step`` を落としても 9 通りのうち 3 通りは偶然格子に乗るので、実測で変異が
        生き残った（seed を固定していないので試行ごとに結果が変わる）。
        """

        distribution = ParameterDistribution(kind="integer", low=2.0, high=10.0, step=4)
        trial = _trial()

        value = distribution.suggest(trial, name=MAX_EPOCHS)

        assert isinstance(value, int)
        assert not isinstance(value, bool)
        assert value in (2, 6, 10)
        registered = trial.distributions[MAX_EPOCHS]
        assert isinstance(registered, optuna.distributions.IntDistribution)
        assert registered.step == 4

    @pytest.mark.parametrize("log", [False, True])
    def test_registers_the_logarithmic_scale_with_optuna(self, log: bool):
        """``log`` を optuna の分布そのものへ渡す."""

        # 値の範囲だけを見る検査では ``log`` を落とした実装を検出できない
        # （偶然範囲内に入る）。実 ``Trial`` が記録した分布を観測する。
        trial = _trial()
        distribution = ParameterDistribution(
            kind="float", low=1.0e-6, high=1.0e-1, log=log
        )

        distribution.suggest(trial, name=LEARNING_RATE)

        registered = trial.distributions[LEARNING_RATE]
        assert isinstance(registered, optuna.distributions.FloatDistribution)
        assert registered.log is log

    def test_suggests_a_logarithmic_integer(self):
        """``log`` スケールの integer は step=1 でだけ成立する（対照）."""

        distribution = ParameterDistribution(
            kind="integer", low=1.0, high=100.0, log=True
        )

        value = distribution.suggest(_trial(), name=MAX_EPOCHS)

        assert isinstance(value, int)
        assert not isinstance(value, bool)
        assert 1 <= value <= 100

    def test_suggests_one_of_the_categorical_choices(self):
        distribution = ParameterDistribution(kind="categorical", choices=("min", "max"))

        value = distribution.suggest(_trial(), name="trainer.mode")

        assert value in ("min", "max")

    def test_suggests_every_dotted_key_of_the_space(self):
        space = SearchSpace(
            parameters={
                LEARNING_RATE: ParameterDistribution(
                    kind="float", low=1.0e-5, high=1.0e-2, log=True
                ),
                MAX_EPOCHS: ParameterDistribution(kind="integer", low=2.0, high=10.0),
            }
        )

        suggested = space.suggest(_trial())

        assert set(suggested) == {LEARNING_RATE, MAX_EPOCHS}


class TestSearchSpaceValidation:
    """空間そのものの整合."""

    def test_reports_an_empty_space(self):
        assert SearchSpace(parameters={}).validate() is not None

    def test_reports_the_name_of_an_inconsistent_parameter(self):
        space = SearchSpace(
            parameters={LEARNING_RATE: ParameterDistribution(kind="float")}
        )

        error = space.validate()

        assert error is not None
        assert LEARNING_RATE in error


class TestSearchSpaceFingerprint:
    """内容だけで決まる探索空間 fingerprint."""

    @staticmethod
    def _parameters(
        *, log: bool = True, high: float = 1.0e-2
    ) -> dict[str, ParameterDistribution]:
        return {
            LEARNING_RATE: ParameterDistribution(
                kind="float", low=1.0e-5, high=high, log=log
            ),
            MAX_EPOCHS: ParameterDistribution(kind="integer", low=2.0, high=10.0),
        }

    def test_ignores_the_insertion_order_of_the_parameters(self):
        forward = self._parameters()
        reversed_order: Mapping[str, ParameterDistribution] = {
            name: forward[name] for name in reversed(list(forward))
        }

        assert (
            SearchSpace(parameters=reversed_order).fingerprint
            == SearchSpace(parameters=forward).fingerprint
        )

    def test_integer_bounds_written_as_int_change_the_fingerprint(self):
        """``validate`` が int の境界を拒否する理由そのものを固定する."""

        as_int = SearchSpace(
            parameters={
                MAX_EPOCHS: ParameterDistribution(kind="integer", low=2, high=10)
            }
        )
        as_float = SearchSpace(
            parameters={
                MAX_EPOCHS: ParameterDistribution(kind="integer", low=2.0, high=10.0)
            }
        )

        assert as_int.fingerprint != as_float.fingerprint
        # 同じ論理空間が別 study 名へ分かれるので、int 版は validate で塞ぐ
        assert as_int.validate() is not None
        assert as_float.validate() is None

    def test_changes_when_a_bound_changes(self):
        assert (
            SearchSpace(parameters=self._parameters(high=1.0e-1)).fingerprint
            != SearchSpace(parameters=self._parameters()).fingerprint
        )

    def test_changes_when_the_logarithmic_flag_changes(self):
        assert (
            SearchSpace(parameters=self._parameters(log=False)).fingerprint
            != SearchSpace(parameters=self._parameters()).fingerprint
        )


class TestSearchSpaceFromToml:
    """TOML 層から strict converter 経由で探索空間になる."""

    def test_structures_a_dotted_config_path_as_a_parameter_name(self, tmp_path: Path):
        layer = tmp_path / "space.toml"
        layer.write_text(
            f'[search_space.parameters."{LEARNING_RATE}"]\n'
            'kind = "float"\n'
            "low = 1.0e-5\n"
            "high = 1.0e-2\n"
            "log = true\n",
            encoding="utf-8",
        )

        value, error = ConfigComposition(layer_paths=(layer,)).structure(
            _Application, converter=make_strict_converter()
        )

        assert error is None
        assert value is not None
        assert set(value.search_space.parameters) == {LEARNING_RATE}
        assert value.search_space.validate() is None

    def test_structures_categorical_choices(self, tmp_path: Path):
        """``choices`` の union hook が要る.

        hook が無いと ``Unsupported type`` で拒否される。

        categorical 分布を TOML から宣言できなくなる。
        """

        layer = tmp_path / "space.toml"
        layer.write_text(
            '[search_space.parameters."trainer.mode"]\n'
            'kind = "categorical"\n'
            'choices = ["min", "max"]\n',
            encoding="utf-8",
        )

        value, error = ConfigComposition(layer_paths=(layer,)).structure(
            _Application, converter=make_strict_converter()
        )

        assert error is None
        assert value is not None
        assert value.search_space.parameters["trainer.mode"].choices == ("min", "max")
        assert value.search_space.validate() is None

    def test_structures_an_integer_distribution(self, tmp_path: Path):
        """境界は TOML でも ``2.0`` と書く（strict converter が int を拒む）."""

        layer = tmp_path / "space.toml"
        layer.write_text(
            f'[search_space.parameters."{MAX_EPOCHS}"]\n'
            'kind = "integer"\n'
            "low = 2.0\n"
            "high = 10.0\n"
            "step = 2\n",
            encoding="utf-8",
        )

        value, error = ConfigComposition(layer_paths=(layer,)).structure(
            _Application, converter=make_strict_converter()
        )

        assert error is None
        assert value is not None
        assert value.search_space.validate() is None
        assert value.search_space.parameters[MAX_EPOCHS].step == 2

    def test_rejects_an_integer_bound_written_without_a_decimal_point(
        self, tmp_path: Path
    ):
        """``low = 2`` は strict converter が拒む（Python 側の validate と一貫する）."""

        layer = tmp_path / "space.toml"
        layer.write_text(
            f'[search_space.parameters."{MAX_EPOCHS}"]\n'
            'kind = "integer"\n'
            "low = 2\n"
            "high = 10.0\n",
            encoding="utf-8",
        )

        value, error = ConfigComposition(layer_paths=(layer,)).structure(
            _Application, converter=make_strict_converter()
        )

        assert value is None
        assert error is not None
        assert "low" in error

    def test_rejects_a_distribution_with_an_unknown_field(self, tmp_path: Path):
        layer = tmp_path / "space.toml"
        layer.write_text(
            f'[search_space.parameters."{LEARNING_RATE}"]\n'
            'kind = "float"\n'
            "lo = 1.0e-5\n"
            "high = 1.0e-2\n",
            encoding="utf-8",
        )

        value, error = ConfigComposition(layer_paths=(layer,)).structure(
            _Application, converter=make_strict_converter()
        )

        assert value is None
        assert error is not None
        assert "lo" in error
