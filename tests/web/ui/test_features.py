"""機能表（`web.ui.features`）の仕様テスト.

機能表はタブ・グループ・テンプレートと、旧 URL の対応を持つ唯一の表。ここで押さえるのは次の 4 つ:

- **表の整合**: slug は URL なので重複させない。タブとテンプレートが実在する
- **機体の機能**: はんだ塗布機のタブ・サイドバー・着地点は作り直し前と同じ。部品実装だけの機体は部品実装タブに着地する
- **backend との縫い目**: 機体の機能ごとに、backend が公開する全ジョブにページがあり、ジョブのページには backend のジョブがある
- **旧 URL**: 作り直し前のブックマーク（``/m/{id}/{旧タブ}/{旧 slug}``）が、すべて新しい行き先を持つ

旧 URL が実際に 307 することは `test_pages.py` が HTTP で見る。
"""

from pathlib import Path

import pytest

from pcbasm.commissioning import steps
from tests.support.web import standalone_catalog
from web.contract.models import Capability
from web.ui import features
from web.ui.features import (
    FEATURES,
    FEATURES_BY_SLUG,
    LEGACY,
    LEGACY_TABS,
    TABS,
    landing,
    machine_tabs,
    tab_features,
)
from web.ui.models import UPDATE_INDEX_PATH

_TEMPLATES_DIR = Path(features.__file__).parent / "templates"
SOLDER: tuple[Capability, ...] = ("solder",)
PNP: tuple[Capability, ...] = ("pnp",)
# 機体の機能の組み合わせ（どちらか 1 つは必ずある）
MACHINES: tuple[tuple[Capability, ...], ...] = (SOLDER, PNP, ("solder", "pnp"))

# 作り直し前の機能のページ。旧 URL は /m/{id}/{旧タブ}/{旧 slug} と /m/{id}/settings。
# 旧 slug は消さない（ブックマークの行き先を失う）
OLD_FEATURES: dict[str, tuple[str, ...]] = {
    "dev": (
        "extract_pcb",
        "make_fill_coverage_pcb",
        "klipper_status",
        "machine_check",
        "audio",
        "config_history",
        "update",
    ),
    "pasting": (
        "paste_solder",
        "height_plane",
        "loading",
        "nozzle_cap",
        "nozzle_clean_test",
        "toolhead_offset",
        "probe_guide",
        "dispense_calibration",
        "paste_volume_calibration",
        "paste_volume_refit",
        "paste_dataset_finalize",
        "paste_test_board",
        "generate_rect_pcb",
    ),
    "posctrl": (
        "camera_preview",
        "copper_detection",
        "camera_calibration",
        "reference_point_setup",
        "orthogonality_test",
        "board_tour",
        "generate_grid_pcb",
    ),
}


def _destination(old: str) -> str:
    return LEGACY.get(old, old)


class TestFeatureTable:
    """表の整合（崩れるとページ描画が 500 になるか、URL が別の機能を指す）."""

    def test_slugs_are_unique(self):
        slugs = [feature.slug for feature in FEATURES]

        assert len(slugs) == len(set(slugs))

    def test_every_feature_belongs_to_a_declared_tab(self):
        assert {feature.tab for feature in FEATURES} == set(TABS)

    @pytest.mark.parametrize("feature", FEATURES, ids=lambda feature: feature.slug)
    def test_template_exists(self, feature: features.Feature):
        assert (_TEMPLATES_DIR / feature.template).is_file()


class TestMachineCapabilities:
    """機体のタブ・サイドバー・着地点は、その機体の機能のページだけで組む."""

    def test_solder_machine_tabs_open_the_same_pages_as_before(self):
        """はんだ塗布機のタブとリンク先は、機能で分ける前と同じ."""
        assert machine_tabs(SOLDER) == {
            "pasting": "塗布",
            "calibration": "校正",
            "board": "基板",
            "machine": "機体",
        }
        assert [tab_features(tab, SOLDER)[0].slug for tab in machine_tabs(SOLDER)] == [
            "paste_solder",
            "commissioning",
            "paste_test_board",
            "klipper_status",
        ]

    def test_pnp_machine_tabs(self):
        """部品実装だけの機体には塗布タブが無く、部品実装タブが先頭になる."""
        assert machine_tabs(PNP) == {
            "pnp": "部品実装",
            "calibration": "校正",
            "board": "基板",
            "machine": "機体",
        }
        assert [tab_features(tab, PNP)[0].slug for tab in machine_tabs(PNP)] == [
            "placement",
            "commissioning",
            "generate_grid_pcb",
            "klipper_status",
        ]

    def test_a_machine_with_both_has_every_tab(self):
        assert machine_tabs(("solder", "pnp")) == TABS

    @pytest.mark.parametrize(
        ("capabilities", "page"),
        [
            (SOLDER, "paste_solder"),
            (PNP, "placement"),
            (("solder", "pnp"), "paste_solder"),
        ],
        ids=["solder", "pnp", "both"],
    )
    def test_landing(self, capabilities: tuple[Capability, ...], page: str):
        """塗布機は日常の塗布、部品実装だけの機体は部品実装のページに着く."""
        assert landing(capabilities) == page

    def test_pnp_machine_has_no_solder_page(self):
        pages = {feature.slug for feature in FEATURES if feature.is_available(PNP)}

        assert {"paste_solder", "nozzle_cap", "paste_test_board"}.isdisjoint(pages)
        assert {"camera_calibration", "machine_check", "machine_config"} <= pages

    def test_sidebar_groups_of_each_tab(self):
        """はんだ塗布機のサイドバーの見出し。表の順に、最初に出た位置へまとめる."""
        groups = {
            tab: list(
                dict.fromkeys(feature.group for feature in tab_features(tab, SOLDER))
            )
            for tab in machine_tabs(SOLDER)
        }

        assert groups == {
            "pasting": ["日常の塗布"],
            "calibration": [
                "立ち上げ",
                "カメラ",
                "位置",
                "ノズル・プローブ",
                "吐出量・塗布量",
            ],
            "board": ["テスト基板", "設計データ"],
            "machine": ["状態と診断", "設定", "通知音", "バックアップと設定履歴"],
        }


class TestCatalogConsistency:
    """Backend のジョブとの縫い目（タブを持つのは frontend だけなので、テスト 1 本で縫う）."""

    @pytest.mark.parametrize("capabilities", MACHINES, ids="+".join)
    def test_job_pages_are_exactly_the_public_jobs(
        self, tmp_path: Path, capabilities: tuple[Capability, ...]
    ):
        """Backend が公開する全ジョブにページがあり、ジョブのページには backend のジョブがある."""
        catalog = standalone_catalog(tmp_path, capabilities)
        jobs = {definition.name for definition in catalog.list()}

        assert {
            feature.slug
            for feature in FEATURES
            if feature.is_job and feature.is_available(capabilities)
        } == jobs


class TestCommissioningSteps:
    """立ち上げの段（``pcbasm.commissioning``）との縫い目."""

    @pytest.mark.parametrize("capabilities", MACHINES, ids="+".join)
    def test_every_step_has_a_page(self, capabilities: tuple[Capability, ...]):
        """チェックリストの各段は、その機体にある、その段を行うページへリンクする."""
        pages = {
            feature.slug for feature in FEATURES if feature.is_available(capabilities)
        }

        assert {step.name for step in steps(capabilities)} <= pages


class TestLegacyUrls:
    """旧 URL の対応表（旧 slug → 新 slug）."""

    @pytest.mark.parametrize(
        "old",
        sorted(
            {
                "settings",
                *OLD_FEATURES,
                *(s for slugs in OLD_FEATURES.values() for s in slugs),
            }
        ),
    )
    def test_every_old_slug_has_a_destination(self, old: str):
        destination = _destination(old)

        assert destination in FEATURES_BY_SLUG or destination == UPDATE_INDEX_PATH

    def test_old_tabs_are_recognized(self):
        assert set(OLD_FEATURES) == LEGACY_TABS

    def test_renamed_slugs(self):
        """名前が変わったのは設定ページとソフトウェア更新だけ（ほかは同じ名前の機能へ飛ぶ）."""
        assert _destination("settings") == "machine_config"
        assert _destination("update") == UPDATE_INDEX_PATH
