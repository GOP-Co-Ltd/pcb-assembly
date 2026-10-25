"""`web.api.jobs.runtime.catalog` の仕様テスト.

契約:

- register は名前重複で ValueError、get は未知ジョブで KeyError
- list は登録順の全件
- validate_params は default 充填済み dict を返す。未知キー・型不一致・
  必須欠落・choice 範囲外は ValueError。optional は省略時にキーを含めない。
  型変換は machine.toml の設定項目と同様
  （bool は value_type="bool" のみ受理、int→float 許容、float→int は整数値のみ）
- filter_persisted_defaults は保存済みのフォーム既定値のうち、いまの persisted_params に
  あって型の合う値だけを残す（パラメータを消したジョブの古い既定値は捨てる）
"""

import pytest

from web.api.jobs.runtime.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.runtime.context import JobContext


def _noop(ctx: JobContext) -> None:
    return None


def _definition(
    name: str = "demo",
    *,
    params: tuple[ParamSpec, ...] = (),
) -> JobDefinition:
    return JobDefinition(
        name=name, label="デモジョブ", run=_noop, params=params, uses_machine=False
    )


class TestRegistry:
    """Register / get / list."""

    def test_register_duplicate_name_raises_value_error(self):
        catalog = JobCatalog()
        catalog.register(_definition("alpha"))

        with pytest.raises(ValueError) as exc:
            catalog.register(_definition("alpha"))

        assert "alpha" in str(exc.value)

    def test_get_unknown_name_raises_key_error(self):
        with pytest.raises(KeyError):
            JobCatalog().get("no-such-job")

    def test_list_returns_all_definitions_in_registration_order(self):
        catalog = JobCatalog()
        catalog.register(_definition("second"))
        catalog.register(_definition("first"))

        names = [definition.name for definition in catalog.list()]

        assert names == ["second", "first"]


_PARAMS = (
    ParamSpec(name="ratio", label="比率", value_type="float", default=1.0),
    ParamSpec(name="count", label="回数", value_type="int", default=3),
    ParamSpec(
        name="mode",
        label="モード",
        value_type="choice",
        default="fast",
        choices=("fast", "slow"),
    ),
    ParamSpec(name="flag", label="フラグ", value_type="bool", default=False),
    ParamSpec(name="title", label="表題", value_type="str"),  # default=None → 必須
    ParamSpec(
        name="position_x",
        label="X",
        value_type="float",
        optional=True,
    ),
)


class TestValidateParams:
    """validate_params の型変換・default 充填・検証."""

    @pytest.fixture
    def catalog(self) -> JobCatalog:
        return JobCatalog()

    @pytest.fixture
    def definition(self) -> JobDefinition:
        return _definition("parametrized", params=_PARAMS)

    def test_defaults_are_filled_for_omitted_keys(
        self, catalog: JobCatalog, definition: JobDefinition
    ):
        values = catalog.validate_params(definition, {"title": "x"})

        assert values == {
            "ratio": 1.0,
            "count": 3,
            "mode": "fast",
            "flag": False,
            "title": "x",
        }

    def test_optional_value_is_coerced_when_supplied(
        self, catalog: JobCatalog, definition: JobDefinition
    ):
        values = catalog.validate_params(definition, {"title": "x", "position_x": 12})

        assert values["position_x"] == 12.0
        assert isinstance(values["position_x"], float)

    def test_int_is_coerced_to_float_for_float_param(
        self, catalog: JobCatalog, definition: JobDefinition
    ):
        values = catalog.validate_params(definition, {"title": "x", "ratio": 2})

        assert values["ratio"] == 2.0
        assert isinstance(values["ratio"], float)

    def test_integral_float_is_coerced_to_int_for_int_param(
        self, catalog: JobCatalog, definition: JobDefinition
    ):
        values = catalog.validate_params(definition, {"title": "x", "count": 4.0})

        assert values["count"] == 4
        assert isinstance(values["count"], int)

    def test_fractional_float_for_int_param_raises_value_error(
        self, catalog: JobCatalog, definition: JobDefinition
    ):
        with pytest.raises(ValueError):
            catalog.validate_params(definition, {"title": "x", "count": 4.5})

    def test_choice_value_within_choices_is_accepted(
        self, catalog: JobCatalog, definition: JobDefinition
    ):
        values = catalog.validate_params(definition, {"title": "x", "mode": "slow"})

        assert values["mode"] == "slow"

    def test_choice_value_outside_choices_raises_value_error(
        self, catalog: JobCatalog, definition: JobDefinition
    ):
        with pytest.raises(ValueError):
            catalog.validate_params(definition, {"title": "x", "mode": "middle"})

    def test_bool_value_for_numeric_param_raises_value_error(
        self, catalog: JobCatalog, definition: JobDefinition
    ):
        # bool は value_type="bool" のみ受理（int のサブクラスでも float 化しない）
        with pytest.raises(ValueError):
            catalog.validate_params(definition, {"title": "x", "ratio": True})

    def test_numeric_value_for_bool_param_raises_value_error(
        self, catalog: JobCatalog, definition: JobDefinition
    ):
        with pytest.raises(ValueError):
            catalog.validate_params(definition, {"title": "x", "flag": 1})

    def test_unknown_key_raises_value_error(
        self, catalog: JobCatalog, definition: JobDefinition
    ):
        with pytest.raises(ValueError) as exc:
            catalog.validate_params(definition, {"title": "x", "nope": 1.0})

        assert "nope" in str(exc.value)

    def test_missing_required_key_raises_value_error(
        self, catalog: JobCatalog, definition: JobDefinition
    ):
        with pytest.raises(ValueError) as exc:
            catalog.validate_params(definition, {})

        assert "title" in str(exc.value)

    def test_wrong_type_raises_value_error(
        self, catalog: JobCatalog, definition: JobDefinition
    ):
        with pytest.raises(ValueError):
            catalog.validate_params(definition, {"title": "x", "count": "many"})


_RUNTIME_PARAMS = (
    # 固定パラメータ（runtime_editable=False）
    ParamSpec(name="board_width", label="基板幅", value_type="float", default=40.0),
    # 実行中変更可（runtime_editable=True）
    ParamSpec(
        name="line_length",
        label="線長",
        value_type="float",
        default=10.0,
        runtime_editable=True,
    ),
    ParamSpec(
        name="line_count",
        label="本数",
        value_type="int",
        default=3,
        runtime_editable=True,
    ),
    ParamSpec(
        name="removal_z_offset",
        label="退避Zオフセット",
        value_type="float",
        default=0.0,
        unit="mm",
        runtime_editable=True,
        minimum=0.0,
    ),
)


class TestValidateRuntimeParams:
    """validate_runtime_params の patch 検証.

    runtime_editable=True のキーだけを coerce して返す patch セマンティクス:
    - default 充填はしない（与えたキーだけ返る）
    - runtime_editable=False の固定キー / 未知キーは ValueError
    - removal_z_offset の負値は ValueError

    型変換そのものは validate_params と同じ `_coerce_param` を共有するため、
    型不一致・int/float 相互変換は `TestValidateParams` 側で固定する。
    """

    @pytest.fixture
    def catalog(self) -> JobCatalog:
        return JobCatalog()

    @pytest.fixture
    def definition(self) -> JobDefinition:
        return _definition("runtime", params=_RUNTIME_PARAMS)

    def test_returns_only_supplied_runtime_keys_without_default_filling(
        self, catalog: JobCatalog, definition: JobDefinition
    ):
        # patch なので line_count / removal_z_offset の default は充填されない
        values = catalog.validate_runtime_params(definition, {"line_length": 12.0})

        assert values == {"line_length": 12.0}

    def test_empty_patch_returns_empty_dict(
        self, catalog: JobCatalog, definition: JobDefinition
    ):
        assert catalog.validate_runtime_params(definition, {}) == {}

    def test_fixed_param_is_rejected(
        self, catalog: JobCatalog, definition: JobDefinition
    ):
        # board_width は runtime_editable=False（キャリブ後固定）
        with pytest.raises(ValueError) as exc:
            catalog.validate_runtime_params(definition, {"board_width": 50.0})

        assert "board_width" in str(exc.value)

    def test_unknown_key_is_rejected(
        self, catalog: JobCatalog, definition: JobDefinition
    ):
        with pytest.raises(ValueError) as exc:
            catalog.validate_runtime_params(definition, {"no_such_param": 1.0})

        assert "no_such_param" in str(exc.value)

    def test_negative_removal_z_offset_is_rejected(
        self, catalog: JobCatalog, definition: JobDefinition
    ):
        # 退避 Z = max(z_min, z_max − offset)。負 offset はパラメータ検証で拒否
        with pytest.raises(ValueError):
            catalog.validate_runtime_params(definition, {"removal_z_offset": -1.0})

    def test_zero_removal_z_offset_is_accepted(
        self, catalog: JobCatalog, definition: JobDefinition
    ):
        # 既定 0.0（= 現状の全退避）は有効
        values = catalog.validate_runtime_params(definition, {"removal_z_offset": 0.0})

        assert values == {"removal_z_offset": 0.0}


class TestRuntimeParamsProperty:
    """JobDefinition.runtime_params（フォーム / router 補助用の name 集合）."""

    def test_lists_only_runtime_editable_names(self):
        definition = _definition("runtime", params=_RUNTIME_PARAMS)

        assert set(definition.runtime_params) == {
            "line_length",
            "line_count",
            "removal_z_offset",
        }


class TestFiniteFloatParams:
    @pytest.mark.parametrize(
        "value",
        [float("inf"), float("-inf"), float("nan"), 10**400],
        ids=["inf", "-inf", "nan", "overflow-int"],
    )
    def test_invalid_numbers_are_rejected_before_running_or_saving(self, value: object):
        catalog = JobCatalog()
        definition = JobDefinition(
            name="finite",
            label="有限数",
            run=_noop,
            uses_machine=False,
            params=(
                ParamSpec("amount", "量", "float", runtime_editable=True, minimum=0.0),
            ),
            persisted_params=("amount",),
        )
        for validate in (catalog.validate_params, catalog.validate_runtime_params):
            with pytest.raises(ValueError, match="amount"):
                validate(definition, {"amount": value})
        assert catalog.filter_persisted_defaults(definition, {"amount": value}) == {}


class TestFilterPersistedDefaults:
    """保存済みのフォーム既定値の読み込み（フォームに出す前に通す）."""

    _DEFINITION = JobDefinition(
        name="persisting",
        label="既定値を保存するジョブ",
        run=_noop,
        uses_machine=False,
        params=(
            ParamSpec("amount", "量", "float", 0.1),
            ParamSpec("count", "回数", "int", 3),
            ParamSpec("note", "メモ", "str", ""),
        ),
        persisted_params=("amount", "count"),
    )

    def test_drops_params_the_job_no_longer_has(self):
        """パラメータを消したジョブでも、古い既定値がフォームへ戻らない."""
        saved = {"amount": 0.2, "tolerance": 0.05}

        kept = JobCatalog().filter_persisted_defaults(self._DEFINITION, saved)

        assert kept == {"amount": 0.2}

    def test_drops_params_that_are_no_longer_persisted(self):
        saved = {"amount": 0.2, "note": "前回のメモ"}

        kept = JobCatalog().filter_persisted_defaults(self._DEFINITION, saved)

        assert kept == {"amount": 0.2}

    def test_drops_values_of_the_wrong_type_and_keeps_the_rest(self):
        saved = {"amount": "多め", "count": 4.0}

        kept = JobCatalog().filter_persisted_defaults(self._DEFINITION, saved)

        assert kept == {"count": 4}
