"""機能表（`web.ui.features`）の仕様テスト.

機能表はタブ・グループ・テンプレートと、旧 URL の対応を持つ唯一の表。ここで押さえるのは次の 3 つ:

- **表の整合**: slug は URL なので重複させない。タブとテンプレートが実在する
- **backend との縫い目**: backend が公開する全ジョブにページがあり、ジョブのページには backend のジョブがある
- **旧 URL**: 作り直し前のブックマーク（``/m/{id}/{旧タブ}/{旧 slug}``）が、すべて新しい行き先を持つ

旧 URL が実際に 307 することは `test_pages.py` が HTTP で見る。
"""

from pathlib import Path

import pytest

from pcbasm.commissioning import STEPS
from tests.support.backend import standalone_catalog
from web.ui import features
from web.ui.features import (
    FEATURES,
    FEATURES_BY_SLUG,
    LANDING,
    LEGACY,
    LEGACY_TABS,
    TABS,
    tab_features,
)
from web.ui.models import UPDATE_INDEX_PATH

_TEMPLATES_DIR = Path(features.__file__).parent / "templates"

# 作り直し前（PR41 より前）の機能のページ。旧 URL は /m/{id}/{旧タブ}/{旧 slug} と /m/{id}/settings。
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

    def test_tab_link_opens_the_first_feature_of_the_tab(self):
        assert [tab_features(tab)[0].slug for tab in TABS] == [
            "paste_solder",
            "commissioning",
            "paste_test_board",
            "klipper_status",
        ]

    def test_sidebar_groups_of_each_tab(self):
        """サイドバーの見出し（blueprint 2.3）。表の順に、最初に出た位置へまとめる."""
        groups = {
            tab: list(dict.fromkeys(feature.group for feature in tab_features(tab)))
            for tab in TABS
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

    def test_landing_is_the_daily_paste_page(self):
        assert LANDING == "paste_solder"
        assert LANDING in FEATURES_BY_SLUG

    @pytest.mark.parametrize("feature", FEATURES, ids=lambda feature: feature.slug)
    def test_template_exists(self, feature: features.Feature):
        assert (_TEMPLATES_DIR / feature.template).is_file()


class TestCatalogConsistency:
    """Backend のジョブとの縫い目（タブを持つのは frontend だけなので、テスト 1 本で縫う）."""

    def test_job_pages_are_exactly_the_public_jobs(self, tmp_path: Path):
        """Backend が公開する全ジョブにページがあり、ジョブのページには backend のジョブがある."""
        jobs = {definition.name for definition in standalone_catalog(tmp_path).list()}

        assert {feature.slug for feature in FEATURES if feature.is_job} == jobs


class TestCommissioningSteps:
    """立ち上げの段（``pcbasm.commissioning``）との縫い目."""

    def test_every_step_has_a_page(self):
        """チェックリストの各段は、その段を行うページへリンクする."""
        assert {step.name for step in STEPS} <= FEATURES_BY_SLUG.keys()


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
