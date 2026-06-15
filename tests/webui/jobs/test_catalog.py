"""`webui.jobs.catalog` の仕様テスト.

計画書 webui-phase3.md「src/webui/jobs/catalog.py」節が契約:

- register は名前重複で ValueError、get は未知ジョブで KeyError
- list は hidden を含む全件（tab で絞り込み可）
- validate_params は default 充填済み dict を返す。未知キー・型不一致・
  必須欠落・choice 範囲外は ValueError。型変換は config_store._coerce と同様
  （bool は value_type="bool" のみ受理、int→float 許容、float→int は整数値のみ）
- default_catalog() は dev 3 ジョブ（2 ジョブ + job_demo）を登録済みで返す
  （fill_path_simulate / generate_grid_pcb は他タブへ移設済み）
"""

from typing import Literal

import pytest

from webui.jobs.catalog import JobCatalog, JobDefinition, ParamSpec, default_catalog
from webui.jobs.context import JobContext


def _noop(ctx: JobContext) -> None:
    return None


def _definition(
    name: str = "demo",
    *,
    tab: Literal["dev", "pasting", "posctrl"] = "dev",
    params: tuple[ParamSpec, ...] = (),
    hidden: bool = False,
) -> JobDefinition:
    return JobDefinition(
        name=name,
        label="デモジョブ",
        tab=tab,
        run=_noop,
        params=params,
        uses_machine=False,
        hidden=hidden,
    )


class TestRegistry:
    """Register / get / list."""

    def test_registered_definition_is_returned_by_get(self):
        catalog = JobCatalog()
        definition = _definition("alpha")

        catalog.register(definition)

        assert catalog.get("alpha") is definition

    def test_register_duplicate_name_raises_value_error(self):
        catalog = JobCatalog()
        catalog.register(_definition("alpha"))

        with pytest.raises(ValueError) as exc:
            catalog.register(_definition("alpha"))

        assert "alpha" in str(exc.value)

    def test_get_unknown_name_raises_key_error(self):
        with pytest.raises(KeyError):
            JobCatalog().get("no-such-job")

    def test_list_returns_all_definitions_including_hidden(self):
        catalog = JobCatalog()
        catalog.register(_definition("visible"))
        catalog.register(_definition("invisible", hidden=True))

        names = {definition.name for definition in catalog.list()}

        assert names == {"visible", "invisible"}

    def test_list_filters_by_tab(self):
        catalog = JobCatalog()
        catalog.register(_definition("dev_job", tab="dev"))
        catalog.register(_definition("pasting_job", tab="pasting"))

        names = {definition.name for definition in catalog.list(tab="pasting")}

        assert names == {"pasting_job"}


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


class TestDefaultCatalog:
    """default_catalog の登録内容（計画書「src/webui/jobs/dev.py」節のピン）."""

    @pytest.fixture
    def catalog(self) -> JobCatalog:
        return default_catalog()

    def test_dev_tab_has_exactly_phase3_jobs(self, catalog: JobCatalog):
        names = {definition.name for definition in catalog.list(tab="dev")}

        assert names == {
            "extract_pcb",
            "make_fill_coverage_pcb",
            "job_demo",
        }

    @pytest.mark.parametrize(
        ("name", "requires_pcb"),
        [
            ("extract_pcb", True),
            ("make_fill_coverage_pcb", False),
            ("job_demo", False),
        ],
    )
    def test_requires_pcb_flags(
        self, catalog: JobCatalog, name: str, requires_pcb: bool
    ):
        assert catalog.get(name).requires_pcb is requires_pcb

    def test_only_job_demo_is_hidden(self, catalog: JobCatalog):
        hidden = {d.name for d in catalog.list(tab="dev") if d.hidden}

        assert hidden == {"job_demo"}

    def test_job_demo_accepts_commands(self, catalog: JobCatalog):
        assert catalog.get("job_demo").accepts_commands is True

    def test_no_dev_job_uses_machine(self, catalog: JobCatalog):
        # Phase 3 の dev ジョブは装置を動かさない（uses_machine=False、情報のみ）
        assert all(not d.uses_machine for d in catalog.list(tab="dev"))
