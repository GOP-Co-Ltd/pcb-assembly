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
from web.api.jobs.catalog import JobCatalog
from web.api.jobs.pasting import register_pasting_jobs
from web.api.models import SettingsField
from web.ui import layout
from web.ui.layout import (
    DISPENSE_CALIBRATION_PARAM_GROUPS,
    FEATURE_TEMPLATES,
    PASTE_VOLUME_CALIBRATION_PARAM_GROUPS,
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

    @pytest.mark.parametrize(
        "table",
        (
            pytest.param(TAB_LABELS, id="base.html の tab_labels[tab]"),
            pytest.param(TAB_PHASES, id="feature ページの TAB_PHASES[tab]"),
        ),
    )
    def test_every_tab_is_covered_by_the_display_tables(self, table: dict[str, str]):
        assert set(TABS) == set(table)

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

    def test_job_templates_are_assigned_to_features(self):
        """ジョブコンテキストを注入する対象は feature のテンプレートに限る."""
        assert layout.JOB_TEMPLATES <= set(FEATURE_TEMPLATES.values())


class TestSoftwareUpdateFeature:
    """ソフトウェア更新は dev タブの feature だが**ジョブではない**.

    計画書「3. frontend」: `TABS["dev"]` / `FEATURE_LABELS` / `FEATURE_TEMPLATES` の
    3 点だけを足して既存 `feature_page` フローに乗せる。`JOB_TEMPLATES` に入れると
    `feature_page` が backend のジョブ定義を要求し、ジョブではないため 503 になる。
    """

    def test_update_is_not_a_job_template(self):
        assert "dev/update.html" not in layout.JOB_TEMPLATES

    def test_both_update_pages_share_one_panel_and_script(self):
        """Backend 側と frontend 自身のページで DOM と JS を分岐させない."""
        for name in ("dev/update.html", "update.html"):
            body = (_TEMPLATES_DIR / name).read_text(encoding="utf-8")
            assert "partials/update_panel.html" in body, name
            assert "js/update.js" in body, name


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


class TestSinglePaneLayout:
    """サイドバーを持たないページは `single-pane` を宣言する.

    `app.css` の `.layout` は「サイドバー / 本文 / マシン制御」の 3 列グリッドで、
    `main-pane` だけを置くと本文が 1 列目（14rem）に押し込まれて読めなくなる。
    `tab.html` 経由のページは 3 枠すべてを埋めるので宣言しない。
    """

    def test_sidebarless_pages_declare_single_pane(self):
        offenders = {
            template.name
            for template in _template_files()
            for text in [template.read_text(encoding="utf-8")]
            if 'extends "base.html"' in text
            and "main-pane" in text
            and "sidebar" not in text
            and "single-pane" not in text
        }

        assert offenders == set()


class TestMachinePrefixFunnel:
    """機体 prefix の付与点は `app.js` の `withBase()` 1 箇所（計画書の設計判断 #4）.

    JS にテストランナーが無く、prefix を素通りする経路（新しい直接 `fetch`・
    prefix を忘れた WS URL・成果物 URL）は静的にしか検出できない。素通りすると
    frontend 経由の操作が同居 backend や 404 に飛ぶ。
    """

    def test_fetch_is_confined_to_the_funnel(self):
        callers = {
            path.name
            for path in _js_files()
            if "fetch(" in path.read_text(encoding="utf-8")
        }

        # app.js = api() / frontendJson() の funnel だけ。通知音は Pi の ALSA で鳴る
        # ので、ブラウザが wav を fetch する経路はもう無い
        assert callers == {"app.js"}

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

    @pytest.mark.parametrize(
        ("keys", "expected"),
        (
            pytest.param(
                (
                    "probe.speed",
                    "probe.retract",
                    "paste_dispenser.pad_align.canny_low",
                    "camera.width",
                ),
                (
                    ("プローブ", (0, 1)),
                    ("ペーストディスペンサー / パッド位置合わせ", (2,)),
                    ("カメラ", (3,)),
                ),
                id="入れ子セクションは最下層で分ける",
            ),
            pytest.param(
                ("camera.width", "probe.speed", "camera.height"),
                (("カメラ", (0,)), ("プローブ", (1,)), ("カメラ", (2,))),
                id="定義順を保つ",
            ),
            pytest.param(
                ("unknown.thing",),
                (("unknown", (0,)),),
                id="未知セクションは生のセクション名",
            ),
        ),
    )
    def test_consecutive_fields_of_a_section_are_grouped_with_its_label(
        self,
        keys: tuple[str, ...],
        expected: tuple[tuple[str, tuple[int, ...]], ...],
    ):
        """入れ子は最下層で分け（親でまとめない）、並びは MACHINE_FIELDS の定義順が正."""
        fields = [self._field(key) for key in keys]

        groups = layout.grouped_fields(fields)

        assert groups == [
            (label, [fields[index] for index in indexes]) for label, indexes in expected
        ]


class TestParamGroupCoverage:
    """フォームのセクション分けがジョブのパラメータを漏れなく含むことのピン.

    書き忘れた項目はフォームから消える（既定値で走るので気付きにくい）。

    存在しない項目名を書くとページ描画が KeyError で 500 になる。
    """

    @pytest.mark.parametrize(
        ("job_name", "groups"),
        [
            ("paste_volume_calibration", PASTE_VOLUME_CALIBRATION_PARAM_GROUPS),
            ("dispense_calibration", DISPENSE_CALIBRATION_PARAM_GROUPS),
        ],
    )
    def test_groups_cover_every_job_param_exactly_once(
        self, job_name: str, groups: tuple[tuple[str, tuple[str, ...]], ...]
    ):
        catalog = JobCatalog()
        register_pasting_jobs(catalog)
        definition = catalog.get(job_name)
        assert definition is not None
        grouped = [name for _, names in groups for name in names]

        assert sorted(grouped) == sorted(spec.name for spec in definition.params)
        assert len(grouped) == len(set(grouped))
