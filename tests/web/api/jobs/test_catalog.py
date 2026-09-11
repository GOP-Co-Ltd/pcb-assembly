"""`web.api.jobs.catalog` の仕様テスト.

計画書 webui-phase3.md「src/webui/jobs/catalog.py」節が契約:

- register は名前重複で ValueError、get は未知ジョブで KeyError
- list は hidden を含む全件（tab で絞り込み可）
- validate_params は default 充填済み dict を返す。未知キー・型不一致・
  必須欠落・choice 範囲外は ValueError。optional は省略時にキーを含めない。
  型変換は config_store._coerce と同様
  （bool は value_type="bool" のみ受理、int→float 許容、float→int は整数値のみ）
- default_catalog() は dev 3 ジョブ（2 ジョブ + job_demo）を登録済みで返す
  （generate_grid_pcb は他タブへ移設済み）
"""

from typing import Literal

import pytest

from web.api.jobs.catalog import JobCatalog, JobDefinition, ParamSpec, default_catalog
from web.api.jobs.context import JobContext


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
    """validate_runtime_params の patch 検証（計画書「能力 1」catalog 節が契約）.

    runtime_editable=True のキーだけを coerce して返す patch セマンティクス:
    - default 充填はしない（与えたキーだけ返る）
    - runtime_editable=False の固定キー / 未知キー / 型不一致は ValueError
    - removal_z_offset の負値は ValueError
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

    def test_multiple_runtime_keys_are_all_coerced(
        self, catalog: JobCatalog, definition: JobDefinition
    ):
        values = catalog.validate_runtime_params(
            definition, {"line_length": 5.0, "line_count": 4}
        )

        assert values == {"line_length": 5.0, "line_count": 4}

    def test_int_param_accepts_integral_float(
        self, catalog: JobCatalog, definition: JobDefinition
    ):
        values = catalog.validate_runtime_params(definition, {"line_count": 4.0})

        assert values["line_count"] == 4
        assert isinstance(values["line_count"], int)

    def test_int_param_rejects_fractional_float(
        self, catalog: JobCatalog, definition: JobDefinition
    ):
        with pytest.raises(ValueError):
            catalog.validate_runtime_params(definition, {"line_count": 4.5})

    def test_float_param_accepts_int(
        self, catalog: JobCatalog, definition: JobDefinition
    ):
        values = catalog.validate_runtime_params(definition, {"line_length": 7})

        assert values["line_length"] == 7.0
        assert isinstance(values["line_length"], float)

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

    def test_type_mismatch_is_rejected(
        self, catalog: JobCatalog, definition: JobDefinition
    ):
        with pytest.raises(ValueError):
            catalog.validate_runtime_params(definition, {"line_length": "abc"})

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

    def test_positive_removal_z_offset_is_accepted(
        self, catalog: JobCatalog, definition: JobDefinition
    ):
        values = catalog.validate_runtime_params(definition, {"removal_z_offset": 3.5})

        assert values == {"removal_z_offset": 3.5}


class TestRuntimeParamsProperty:
    """JobDefinition.runtime_params（フォーム / router 補助用の name 集合）."""

    def test_lists_only_runtime_editable_names(self):
        definition = _definition("runtime", params=_RUNTIME_PARAMS)

        assert set(definition.runtime_params) == {
            "line_length",
            "line_count",
            "removal_z_offset",
        }

    def test_is_empty_when_no_runtime_editable_params(self):
        definition = _definition(
            "fixed",
            params=(
                ParamSpec(
                    name="board_width", label="幅", value_type="float", default=1.0
                ),
            ),
        )

        assert definition.runtime_params == ()


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

    def test_long_running_paste_jobs_notify_on_completion(self, catalog: JobCatalog):
        """ブラウザの完了通知バナーの対象（機体スピーカーは uses_machine で決まる）."""
        notifying = {
            definition.name
            for definition in catalog.list()
            if definition.notify_on_completion
        }

        assert notifying == {"paste_solder", "paste_volume_calibration"}

    def test_machine_jobs_define_the_completion_sound_scope(self, catalog: JobCatalog):
        """機体スピーカーの完了音（成功 / 失敗）が鳴るジョブの全量.

        `uses_machine` は終了時の駐機だけでなく完了音の対象も決めるため、集合を
        仕様として固定する（新ジョブの追加で意図せず鳴る / 鳴らないを検知する）。
        """
        machine_jobs = {
            definition.name for definition in catalog.list() if definition.uses_machine
        }

        assert machine_jobs == {
            "board_tour",
            "camera_calibration",
            "dispense_calibration",
            "height_plane",
            "loading",
            "orthogonality_test",
            "paste_solder",
            "paste_volume_calibration",
            "reference_point_setup",
            "toolhead_offset",
        }
