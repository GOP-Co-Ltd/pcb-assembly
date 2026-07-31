"""`web.ui.layout` の表示知識と、移設した静的資産・テンプレートの整合.

計画書 docs/plans/web-api-ui-split.md「MR4」節が契約:

- `pages.py` の**純粋な表示知識だけ**を frontend へ移す（装置の事実は backend が
  `JobSpecInfo` で自己申告する）
- `SECTION_LABELS` / `section_of` は設定ページの階層表示専用なので frontend が持つ。
  ラベルが欠けると設定ページの見出しに生のドットキーが出る
- `templates/` と `static/` は `web.api` から `web.ui` へ移設する

テンプレート名・静的資産名の誤りはページ描画時の 500 やアセット 404 になり、
pyright も pytest も文字列としては検出しないためここでピンする。
"""

import re
from pathlib import Path

import pytest

from web.api.config_store import MACHINE_FIELDS
from web.api.models import SettingsField
from web.ui import layout
from web.ui.layout import (
    FEATURE_TEMPLATES,
    SECTION_LABELS,
    TAB_LABELS,
    TAB_PHASES,
    TABS,
    section_of,
)

_UI_DIR = Path(layout.__file__).parent
_TEMPLATES_DIR = _UI_DIR / "templates"
_STATIC_DIR = _UI_DIR / "static"
_JS_DIR = _STATIC_DIR / "js"

# base.html などの {{ static_asset('js/app.js') }} から参照アセット名を取り出す
_STATIC_ASSET_CALL = re.compile(r"static_asset\(\s*'([^']+)'\s*\)")


def _template_files() -> list[Path]:
    return sorted(_TEMPLATES_DIR.rglob("*.html"))


def _js_files() -> list[Path]:
    return sorted(_JS_DIR.rglob("*.js"))


def _js_code_lines(marker: str) -> list[str]:
    """JS 全体から marker を含むコード行（行コメントを除く）を集める."""
    return [
        line
        for path in _js_files()
        for line in path.read_text(encoding="utf-8").splitlines()
        if marker in line and not line.lstrip().startswith("//")
    ]


class TestTabs:
    """タブの表示知識（欠けるとヘッダ描画やページ描画が KeyError で 500 する）."""

    def test_every_tab_has_a_label(self):
        """base.html は tab_labels[tab] を引く."""
        assert set(TABS) == set(TAB_LABELS)

    def test_every_tab_has_a_phase(self):
        """Feature ページは TAB_PHASES[tab] を引く."""
        assert set(TABS) == set(TAB_PHASES)

    def test_feature_slugs_are_unique_across_tabs(self):
        """Feature slug は URL とジョブ名の突き合わせに使うので重複させない."""
        slugs = [slug for features in TABS.values() for slug in features]

        assert len(slugs) == len(set(slugs))


class TestFeatureTemplates:
    """テンプレート割り当て（存在しない名前は描画時 500 になる）."""

    def test_keys_are_declared_features(self):
        assert all(
            feature in TABS.get(tab, ()) for tab, feature in FEATURE_TEMPLATES
        ), FEATURE_TEMPLATES.keys()

    @pytest.mark.parametrize("name", sorted(set(FEATURE_TEMPLATES.values())))
    def test_template_file_exists(self, name: str):
        assert (_TEMPLATES_DIR / name).is_file()

    def test_placeholder_template_exists(self):
        """FEATURE_TEMPLATES に無い feature のフォールバック."""
        assert (_TEMPLATES_DIR / "feature.html").is_file()

    def test_job_templates_are_assigned_to_features(self):
        """ジョブコンテキストを注入する対象は feature のテンプレートに限る."""
        assert layout.JOB_TEMPLATES <= set(FEATURE_TEMPLATES.values())


class TestStaticAssets:
    """テンプレートが参照する静的資産が移設先に存在する."""

    def test_referenced_assets_exist(self):
        missing = {
            asset
            for template in _template_files()
            for asset in _STATIC_ASSET_CALL.findall(
                template.read_text(encoding="utf-8")
            )
            if not (_STATIC_DIR / asset).is_file()
        }

        assert missing == set()

    def test_machine_selector_is_wired_into_every_page(self):
        """マシン切替は全ページ共通なので base.html から読み込む."""
        base_html = (_TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")

        assert "js/machine_selector.js" in base_html
        assert "partials/machine_selector.html" in base_html


class TestMachinePrefixFunnel:
    """機体 prefix の付与点は `app.js` の `withBase()` 1 箇所（計画書の設計判断 #4）.

    JS にテストランナーが無く、prefix を素通りする経路（新しい直接 `fetch`・
    prefix を忘れた WS URL・成果物 URL）は静的にしか検出できない。素通りすると
    frontend 経由の操作が同居 backend や 404 に飛ぶ。
    """

    def test_fetch_is_confined_to_the_funnel_and_the_completion_sound(self):
        callers = {
            path.name
            for path in _js_files()
            if "fetch(" in path.read_text(encoding="utf-8")
        }

        # app.js = api() の funnel、job_console.js = 完了音（/static は prefix しない）
        assert callers == {"app.js", "job_console.js"}

    def test_the_funnel_prefixes_from_the_body_dataset(self):
        app_js = (_JS_DIR / "app.js").read_text(encoding="utf-8")

        assert "document.body.dataset.machineBase" in app_js
        assert "fetch(withBase(url)" in app_js

    def test_websocket_url_is_prefixed(self):
        lines = _js_code_lines("new WebSocket(")

        assert len(lines) == 1
        assert "withBase(" in lines[0]

    def test_artifact_urls_are_prefixed_once(self):
        lines = _js_code_lines("artifact.url")

        assert len(lines) == 1
        assert "withBase(artifact.url)" in lines[0]


class TestSectionLabels:
    """設定セクションのラベル（frontend だけが持つ表示知識）."""

    def test_every_machine_field_section_has_a_label(self):
        """ラベルが無いと設定ページの見出しに生のドットキーが出る."""
        unlabeled = {
            section_of(spec.key)
            for spec in MACHINE_FIELDS
            if section_of(spec.key) not in SECTION_LABELS
        }

        assert unlabeled == set()


class TestGroupedFields:
    """設定項目のセクション分け."""

    @staticmethod
    def _field(key: str) -> SettingsField:
        return SettingsField(
            key=key, label=key, value_type="float", unit=None, value=1.0, resolved=1.0
        )

    def test_consecutive_fields_of_a_section_are_grouped_with_its_label(self):
        """入れ子セクションは最下層で分ける（親でまとめない）."""
        fields = [
            self._field("probe.speed"),
            self._field("probe.retract"),
            self._field("paste_dispenser.pad_align.canny_low"),
            self._field("camera.width"),
        ]

        groups = layout.grouped_fields(fields)

        assert groups == [
            ("プローブ", fields[:2]),
            ("ペーストディスペンサー / パッド位置合わせ", fields[2:3]),
            ("カメラ", fields[3:]),
        ]

    def test_definition_order_is_preserved(self):
        """Settings ページの並びは MACHINE_FIELDS の定義順が正."""
        fields = [
            self._field("camera.width"),
            self._field("probe.speed"),
            self._field("camera.height"),
        ]

        labels = [label for label, _ in layout.grouped_fields(fields)]

        assert labels == ["カメラ", "プローブ", "カメラ"]

    def test_unknown_section_falls_back_to_the_raw_section(self):
        groups = layout.grouped_fields([self._field("unknown.thing")])

        assert groups == [("unknown", [self._field("unknown.thing")])]
