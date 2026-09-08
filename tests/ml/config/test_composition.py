"""TOML 層と CLI 上書きから設定を合成する境界の公開契約.

計画 §13.1 / §15.1 に対応する。合成は例外を投げず、失敗をすべて理由文字列で返す。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import attrs
import pytest

from ml.config.composition import ConfigComposition
from ml.serialization import make_strict_converter


@attrs.frozen
class _Trainer:
    """合成の到達先にする、全フィールドが既定値を持つ設定."""

    learning_rate: float = 1e-3
    max_epochs: int = 30
    monitor: str = "loss"
    mode: Literal["min", "max"] = "min"
    deterministic: bool = True


@attrs.frozen
class _Application:
    """節 (`trainer`) を持つ入れ子の設定."""

    trainer: _Trainer = _Trainer()
    run_kind: str = "train"


def _write(path: Path, text: str) -> Path:
    """TOML の層を 1 つ書き出す."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestComposeLayers:
    """層の読み込みと後勝ちの再帰 merge."""

    def test_composes_an_empty_mapping_without_layers_or_overrides(self):
        data, error = ConfigComposition().compose()

        assert error is None
        assert data == {}

    def test_reads_a_single_toml_layer(self, tmp_path: Path):
        layer = _write(
            tmp_path / "base.toml",
            '[trainer]\nlearning_rate = 1.0e-4\nmonitor = "score"\n',
        )

        data, error = ConfigComposition(layer_paths=(layer,)).compose()

        assert error is None
        assert data == {"trainer": {"learning_rate": 1e-4, "monitor": "score"}}

    def test_merges_later_layers_over_earlier_ones_recursively(self, tmp_path: Path):
        base = _write(
            tmp_path / "base.toml",
            "[trainer]\nlearning_rate = 1.0e-3\nmax_epochs = 30\n",
        )
        edge = _write(tmp_path / "edge.toml", "[trainer]\nlearning_rate = 2.0e-4\n")

        data, error = ConfigComposition(layer_paths=(base, edge)).compose()

        assert error is None
        # 後の層が持たない ``max_epochs`` は残る。素の ``dict.update`` だと消える
        assert data == {"trainer": {"learning_rate": 2e-4, "max_epochs": 30}}

    def test_replaces_lists_instead_of_concatenating_them(self, tmp_path: Path):
        base = _write(tmp_path / "base.toml", "[trainer]\nstages = [1, 2, 3]\n")
        edge = _write(tmp_path / "edge.toml", "[trainer]\nstages = [9]\n")

        data, error = ConfigComposition(layer_paths=(base, edge)).compose()

        assert error is None
        assert data == {"trainer": {"stages": [9]}}

    def test_reports_a_missing_layer_path(self, tmp_path: Path):
        data, error = ConfigComposition(
            layer_paths=(tmp_path / "absent.toml",)
        ).compose()

        assert data is None
        assert error is not None
        assert "absent.toml" in error

    def test_reports_a_malformed_toml_layer(self, tmp_path: Path):
        layer = _write(tmp_path / "broken.toml", "[trainer\nlearning_rate = 1.0e-4\n")

        data, error = ConfigComposition(layer_paths=(layer,)).compose()

        assert data is None
        assert error is not None
        assert "broken.toml" in error


class TestApplyOverrides:
    """``key=value`` 形式の上書きと、その値の解釈."""

    def test_replaces_an_existing_value(self, tmp_path: Path):
        layer = _write(tmp_path / "base.toml", "[trainer]\nlearning_rate = 1.0e-3\n")

        data, error = ConfigComposition(
            layer_paths=(layer,), overrides=("trainer.learning_rate=2.0e-4",)
        ).compose()

        assert error is None
        assert data == {"trainer": {"learning_rate": 2e-4}}

    def test_creates_missing_intermediate_tables(self):
        data, error = ConfigComposition(
            overrides=('trainer.compile_options.mode="default"',)
        ).compose()

        assert error is None
        assert data == {"trainer": {"compile_options": {"mode": "default"}}}

    @pytest.mark.parametrize(
        ("override", "expected"),
        [
            ("value=true", True),
            ("value=1.0e-4", 1e-4),
            ("value=7", 7),
            ('value="x"', "x"),
            ("value=[1, 2]", [1, 2]),
            # shell がクォートを食うので、裸の文字列も ``str`` として受ける（裁定 1）
            ("value=mae", "mae"),
            ("value=max", "max"),
        ],
    )
    def test_interprets_values_with_the_toml_scalar_rules(
        self, override: str, expected: object
    ):
        data, error = ConfigComposition(overrides=(override,)).compose()

        assert error is None
        assert data is not None
        # ``bool`` は ``int`` の部分型なので、型そのものを固定する
        assert type(data["value"]) is type(expected)
        assert data["value"] == expected

    def test_reports_a_token_without_a_separator(self):
        data, error = ConfigComposition(overrides=("trainer.learning_rate",)).compose()

        assert data is None
        assert error is not None
        assert "trainer.learning_rate" in error

    def test_reports_a_token_with_an_empty_key(self):
        data, error = ConfigComposition(overrides=("=1",)).compose()

        assert data is None
        assert error is not None

    def test_accepts_a_bare_string_without_quotes(self):
        """クォート無しの文字列を ``str`` として受ける（裁定 1）.

        shell がクォートを食うため、``monitor=mae`` は二重クォートなしで届く。

        型の正しさを守るのは合成層ではなく strict converter 側とする。
        """

        data, error = ConfigComposition(overrides=("trainer.monitor=mae",)).compose()

        assert error is None
        assert data == {"trainer": {"monitor": "mae"}}

    @pytest.mark.parametrize(
        "override",
        [
            'trainer.monitor="loss',
            "trainer.monitor='loss",
            'trainer.monitor="',
        ],
    )
    def test_reports_a_value_whose_quote_is_not_closed(self, override: str):
        """引用符で始まる token は裸の文字列として黙って採用しない.

        裸の文字列に引用符は現れないので、閉じ忘れだと確定できる。到達先が ``str``
        なら strict converter も守れない。
        """

        data, error = ConfigComposition(overrides=(override,)).compose()

        assert data is None
        assert error is not None
        assert "引用符" in error

    def test_still_accepts_a_bare_string_that_contains_a_quote_later(self):
        """引用符で始まらない token は従来どおり裸の文字列として通す（対照）."""

        data, error = ConfigComposition(overrides=('trainer.monitor=loss"x',)).compose()

        assert error is None
        assert data == {"trainer": {"monitor": 'loss"x'}}

    def test_reports_an_intermediate_key_that_is_not_a_table(self):
        data, error = ConfigComposition(
            overrides=("trainer=1", 'trainer.monitor="x"')
        ).compose()

        assert data is None
        assert error is not None
        assert "trainer" in error


class TestFromArguments:
    """引数列を group 選択と上書きへ振り分ける規則."""

    @staticmethod
    def _configuration_root(tmp_path: Path) -> Path:
        root = tmp_path / "conf"
        _write(
            root / "base.toml", 'shared = "base"\n[trainer]\nlearning_rate = 1.0e-3\n'
        )
        _write(
            root / "trainer" / "edge.toml",
            'shared = "trainer"\n[trainer]\nlearning_rate = 2.0e-4\n',
        )
        _write(root / "optimizer" / "adam.toml", 'shared = "optimizer"\n')
        return root

    def test_treats_a_token_whose_name_is_an_existing_directory_as_a_group(
        self, tmp_path: Path
    ):
        root = self._configuration_root(tmp_path)

        composition, error = ConfigComposition.from_arguments(
            ("trainer=edge",), configuration_root=root
        )

        assert error is None
        assert composition is not None
        assert composition.layer_paths == (root / "trainer" / "edge.toml",)
        assert composition.overrides == ()

    def test_treats_a_token_whose_name_is_not_a_directory_as_an_override(
        self, tmp_path: Path
    ):
        root = self._configuration_root(tmp_path)

        composition, error = ConfigComposition.from_arguments(
            ("run_kind=finetune",), configuration_root=root
        )

        assert error is None
        assert composition is not None
        assert composition.layer_paths == ()
        assert composition.overrides == ("run_kind=finetune",)

    def test_stacks_base_layers_before_group_layers(self, tmp_path: Path):
        root = self._configuration_root(tmp_path)

        composition, error = ConfigComposition.from_arguments(
            ("trainer=edge",), configuration_root=root, base_names=("base",)
        )

        assert error is None
        assert composition is not None
        assert composition.layer_paths == (
            root / "base.toml",
            root / "trainer" / "edge.toml",
        )
        data, compose_error = composition.compose()
        assert compose_error is None
        assert data is not None
        # group 層が base 層より後に積まれているので group 側が勝つ
        assert data["shared"] == "trainer"

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            (("trainer=edge", "optimizer=adam"), "optimizer"),
            (("optimizer=adam", "trainer=edge"), "trainer"),
        ],
    )
    def test_stacks_group_layers_in_argument_order(
        self, tmp_path: Path, arguments: tuple[str, ...], expected: str
    ):
        root = self._configuration_root(tmp_path)

        composition, error = ConfigComposition.from_arguments(
            arguments, configuration_root=root
        )

        assert error is None
        assert composition is not None
        data, compose_error = composition.compose()
        assert compose_error is None
        assert data is not None
        assert data["shared"] == expected

    def test_reports_a_group_whose_option_toml_is_missing(self, tmp_path: Path):
        root = self._configuration_root(tmp_path)

        composition, error = ConfigComposition.from_arguments(
            ("trainer=absent",), configuration_root=root
        )

        assert composition is None
        assert error is not None
        assert "absent" in error


class TestStructure:
    """合成した dict を strict converter で frozen attrs へ落とす境界."""

    def test_structures_attrs_defaults_when_nothing_is_composed(self):
        value, error = ConfigComposition().structure(
            _Application, converter=make_strict_converter()
        )

        assert error is None
        assert value == _Application()

    def test_structures_a_diff_layer_and_an_override(self, tmp_path: Path):
        layer = _write(tmp_path / "edge.toml", "[trainer]\nmax_epochs = 60\n")

        value, error = ConfigComposition(
            layer_paths=(layer,), overrides=("trainer.learning_rate=2.0e-4",)
        ).structure(_Application, converter=make_strict_converter())

        assert error is None
        assert value == _Application(
            trainer=_Trainer(learning_rate=2e-4, max_epochs=60)
        )

    def test_rejects_an_unknown_key(self, tmp_path: Path):
        layer = _write(tmp_path / "edge.toml", "[trainer]\nlerning_rate = 1.0e-4\n")

        value, error = ConfigComposition(layer_paths=(layer,)).structure(
            _Application, converter=make_strict_converter()
        )

        assert value is None
        assert error is not None
        assert "lerning_rate" in error

    def test_rejects_an_integer_for_a_float_field(self):
        """決定 3 の pin。合成層に int から float への昇格を入れてはならない.

        昇格を入れると「記録した値と実際に使われた値が食い違わない」という strict converter
        の意図が薄れ、``bool`` を ``int`` として弾いている機構も緩む。
        """

        value, error = ConfigComposition(
            overrides=("trainer.learning_rate=1",)
        ).structure(_Application, converter=make_strict_converter())

        assert value is None
        assert error is not None
        assert "learning_rate" in error
        assert "float" in error

    def test_rejects_a_bare_string_for_an_integer_field(self):
        """型の誤りは合成層ではなく converter が捕まえる（裁定 1）.

        裸の文字列を受けるようにした結果、``max_epochs=abc`` は ``str`` として
        合成を通る。

        拒否する機構は strict converter 側に移る。
        """

        value, error = ConfigComposition(
            overrides=("trainer.max_epochs=abc",)
        ).structure(_Application, converter=make_strict_converter())

        assert value is None
        assert error is not None
        assert "max_epochs" in error
        assert "int" in error

    def test_accepts_a_bare_string_for_a_literal_field(self):
        value, error = ConfigComposition(overrides=("trainer.mode=max",)).structure(
            _Application, converter=make_strict_converter()
        )

        assert error is None
        assert value == _Application(trainer=_Trainer(mode="max"))

    def test_rejects_a_string_outside_a_literal_field(self):
        value, error = ConfigComposition(overrides=("trainer.mode=maximum",)).structure(
            _Application, converter=make_strict_converter()
        )

        assert value is None
        assert error is not None
        assert "mode" in error

    def test_accepts_a_float_field_written_with_a_decimal_point(self):
        """上のテストの対照。「float は小数点か指数を必ず書く」規約の根拠."""

        value, error = ConfigComposition(
            overrides=("trainer.learning_rate=1.0",)
        ).structure(_Application, converter=make_strict_converter())

        assert error is None
        assert value == _Application(trainer=_Trainer(learning_rate=1.0))
