"""全ジョブのカタログ（`web.api.jobs.catalog.default_catalog`）の登録内容.

ジョブ名・ファイル名・画面の feature slug は同じ語で、ジョブのディレクトリは呼ぶ pcbasm のドメインをミラーする。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.support.web import standalone_catalog
from web.api.jobs.runtime.catalog import JobCatalog
from web.api.jobs.steps import LOADING_STAGE

JOBS_BY_DOMAIN = {
    "align": (
        "reference_point_setup",
        "camera_calibration",
        "board_tour",
        "orthogonality_test",
    ),
    "pasting": (
        "paste_solder",
        "loading",
        "height_plane",
        "toolhead_offset",
        "nozzle_clean_test",
        "dispense_calibration",
        "paste_volume_calibration",
        "paste_volume_refit",
        "paste_dataset_finalize",
    ),
    "kicad": (
        "extract_pcb",
        "generate_grid_pcb",
        "generate_rect_pcb",
        "make_fill_coverage_pcb",
    ),
    "machine": ("machine_check",),
}

JOBS_DIR = Path(__file__).parents[4] / "src" / "web" / "api" / "jobs"


@pytest.fixture
def catalog(tmp_path: Path) -> JobCatalog:
    return standalone_catalog(tmp_path)


class TestJobList:
    def test_registers_exactly_the_jobs(self, catalog: JobCatalog):
        names = [definition.name for definition in catalog.list()]

        assert sorted(names) == sorted(
            name for jobs in JOBS_BY_DOMAIN.values() for name in jobs
        )

    @pytest.mark.parametrize(
        ("domain", "name"),
        [(domain, name) for domain, jobs in JOBS_BY_DOMAIN.items() for name in jobs],
    )
    def test_job_lives_in_a_file_named_after_it(self, domain: str, name: str):
        """ジョブ名で grep すれば、ジョブの定義と台本に 1 回で着く."""
        assert (JOBS_DIR / domain / f"{name}.py").is_file()

    @pytest.mark.parametrize(
        ("domain", "name"),
        [(domain, name) for domain, jobs in JOBS_BY_DOMAIN.items() for name in jobs],
    )
    def test_job_does_not_import_another_job(self, domain: str, name: str):
        """共有の部品は steps.py にあり、別のジョブのファイルを辿らずに済む."""
        jobs = {
            f"web.api.jobs.{other_domain}.{other}"
            for other_domain, others in JOBS_BY_DOMAIN.items()
            for other in others
        }
        tree = ast.parse((JOBS_DIR / domain / f"{name}.py").read_text())
        imported = {
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        }

        assert imported & jobs == set()

    @pytest.mark.parametrize(
        ("name", "requires_pcb", "uses_machine", "accepts_commands"),
        [
            ("reference_point_setup", False, True, True),
            ("camera_calibration", False, True, False),
            ("board_tour", True, True, False),
            ("orthogonality_test", True, True, False),
            ("paste_solder", True, True, True),
            ("loading", False, True, True),
            ("height_plane", True, True, False),
            ("toolhead_offset", True, True, True),
            ("nozzle_clean_test", False, True, False),
            ("dispense_calibration", False, True, True),
            ("paste_volume_calibration", False, True, True),
            ("paste_volume_refit", False, False, False),
            ("paste_dataset_finalize", False, False, False),
            ("extract_pcb", True, False, False),
            ("generate_grid_pcb", False, False, False),
            ("generate_rect_pcb", False, False, False),
            ("make_fill_coverage_pcb", False, False, False),
            ("machine_check", False, False, False),
        ],
    )
    def test_job_flags(
        self,
        catalog: JobCatalog,
        name: str,
        requires_pcb: bool,
        uses_machine: bool,
        accepts_commands: bool,
    ):
        definition = catalog.get(name)

        assert definition.requires_pcb is requires_pcb
        assert definition.uses_machine is uses_machine
        assert definition.accepts_commands is accepts_commands

    def test_long_running_paste_jobs_notify_on_completion(self, catalog: JobCatalog):
        """ブラウザの完了通知バナーの対象（機体スピーカーは uses_machine で決まる）."""
        notifying = {
            definition.name
            for definition in catalog.list()
            if definition.notify_on_completion
        }

        assert notifying == {"paste_solder", "paste_volume_calibration"}


class TestLoadingPanel:
    """ローディング欄を出すジョブと、その欄の宣言."""

    def test_jobs_with_a_loading_panel(self, catalog: JobCatalog):
        """ローディング欄のあるジョブは command を受ける（欄のボタンが command を送る）."""
        with_panel = {
            definition.name
            for definition in catalog.list()
            if definition.loading is not None
        }

        assert with_panel == {
            "paste_solder",
            "loading",
            "toolhead_offset",
            "dispense_calibration",
            "paste_volume_calibration",
        }
        assert all(catalog.get(name).accepts_commands for name in with_panel)

    def test_panel_defaults_come_from_the_jobs_own_params(self, catalog: JobCatalog):
        """欄の既定値に使う ParamSpec がジョブにある（名前を変えると欄が既定値を失う）."""
        for definition in catalog.list():
            panel = definition.loading
            if panel is None:
                continue
            names = {spec.name for spec in definition.params}
            assert panel.amount in names, definition.name
            if panel.rotation is not None:
                assert {
                    panel.rotation.rotations,
                    panel.rotation.rate,
                    panel.rotation.accel,
                    panel.rotation.retract_rotations,
                } <= names, definition.name

    def test_rotation_section_is_shown_where_the_form_has_rotation_params(
        self, catalog: JobCatalog
    ):
        rotating = {
            definition.name
            for definition in catalog.list()
            if definition.loading is not None and definition.loading.rotation
        }

        assert rotating == {"loading", "paste_volume_calibration"}

    def test_dispense_calibration_primes_from_the_menu_too(self, catalog: JobCatalog):
        """吐出量キャリブはメニュー段階のプライムと ① 専用ローディング段階の両方でボタンを押せる."""
        panel = catalog.get("dispense_calibration").loading

        assert panel is not None
        assert panel.stages == ("キャリブレーションメニュー", LOADING_STAGE)


class TestParamGroups:
    """フォーム内のまとまり（ParamSpec.group）."""

    @pytest.mark.parametrize(
        "name", ["dispense_calibration", "paste_volume_calibration"]
    )
    def test_calibration_forms_group_every_param(self, catalog: JobCatalog, name: str):
        """項目の多い校正ジョブのフォームは、すべての項目がどこかのまとまりに入る."""
        ungrouped = [
            spec.name for spec in catalog.get(name).params if spec.group is None
        ]

        assert ungrouped == []

    def test_detection_hyperparameters_are_advanced(self, catalog: JobCatalog):
        """円検出のハイパラは普段は触らないので、収集ジョブのフォームでは畳む."""
        advanced = {
            spec.name
            for spec in catalog.get("paste_volume_calibration").params
            if spec.group is not None and spec.group.advanced
        }

        assert advanced == {
            "min_contrast",
            "contrast_percentile",
            "threshold_floor_ratio",
            "open_kernel_px",
            "min_area_px",
            "require_blank_zero",
        }
