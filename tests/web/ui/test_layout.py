"""`web.ui.layout` の表示知識と、移設した静的資産・テンプレートの整合.

計画書 docs/plans/web-api-ui-split.md「MR4」節が契約:

- `pages.py` の**純粋な表示知識だけ**を frontend へ移す（装置の事実は backend が
  `JobSpecInfo` で自己申告する）
- 設定ページの項目・見出し・並び順は backend が返し、`settings_sections` はまとめるだけ
- `templates/` と `static/` は `web.api` から `web.ui` へ移設する

テンプレート名・静的資産名の誤りはページ描画時の 500 やアセット 404 になり、
pyright も pytest も文字列としては検出しないためここでピンする。
"""

import re
from pathlib import Path

import pytest

from tests.support.web import standalone_catalog
from web.contract.models import ParamGroupInfo, ParamSpecInfo, SettingsField
from web.ui import layout

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


class TestUpdatePanel:
    """更新ページは UI サーバーと機体で同じパネルと JS を使う（DOM と JS を分岐させない）."""

    def test_update_page_shares_one_panel_and_script(self):
        body = (_TEMPLATES_DIR / "update.html").read_text(encoding="utf-8")

        assert body.count('{% include "partials/update_panel.html" %}') == 2
        assert "js/update.js" in body


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
    `feature.html` 経由のページは 3 枠すべてを埋めるので宣言しない。
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
    """機体 prefix の付与点は `api.js` の `withBase()` 1 箇所（計画書の設計判断 #4）.

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

        # api.js = api() / frontendJson() の funnel だけ。通知音は Pi の ALSA で鳴る
        # ので、ブラウザが wav を fetch する経路はもう無い
        assert callers == {"api.js"}

    def test_the_funnel_prefixes_from_the_body_dataset(self):
        api_js = (_JS_DIR / "api.js").read_text(encoding="utf-8")

        assert "document.body.dataset.machineBase" in api_js
        assert "fetch(withBase(url)" in api_js

    def test_websocket_is_opened_in_one_place_with_the_prefix(self):
        """WS を張るのは ws.js だけ（機体のページも機体一覧もそれを使う）."""
        callers = {
            path.name
            for path in _js_files()
            if "new WebSocket(" in path.read_text(encoding="utf-8")
        }
        lines = _js_code_lines("new WebSocket(")

        assert callers == {"ws.js"}
        assert len(lines) == 1
        assert "withBase(" in lines[0]

    def test_artifact_urls_are_prefixed_once(self):
        lines = _js_code_lines("artifact.url")

        assert len(lines) == 1
        assert "withBase(artifact.url)" in lines[0]


def _field(key: str, section: str, group: str) -> SettingsField:
    return SettingsField(
        key=key,
        section=section,
        section_label=section.upper(),
        group=group,
        label=key,
        value_type="float",
        unit=None,
        choices=[],
        minimum=None,
        clearable=False,
        value=1.0,
        resolved=1.0,
    )


class TestSettingsSections:
    """`settings_sections()` は backend が返した順のまま、画面（section）とカード（group）へまとめる."""

    def test_groups_fields_by_section_and_card_in_the_backend_order(self):
        fields = [
            _field("camera.width", "camera", "デバイス"),
            _field("camera.crop.width", "camera", "クロップ"),
            _field("camera.height", "camera", "デバイス"),
            _field("audio.volume", "audio", "通知音"),
        ]

        sections = layout.settings_sections(fields)

        assert [
            (
                section.slug,
                section.label,
                [
                    (group.label, [field.key for field in group.fields])
                    for group in section.groups
                ],
            )
            for section in sections
        ] == [
            (
                "camera",
                "CAMERA",
                [
                    ("デバイス", ["camera.width", "camera.height"]),
                    ("クロップ", ["camera.crop.width"]),
                ],
            ),
            ("audio", "AUDIO", [("通知音", ["audio.volume"])]),
        ]
        # ナビのバッジ（field_count）は画面の項目数
        assert [section.field_count for section in sections] == [3, 1]


def _spec(name: str, group: ParamGroupInfo | None) -> ParamSpecInfo:
    return ParamSpecInfo(name=name, label=name, value_type="float", group=group)


class TestParamSections:
    """`param_sections()` は backend が返した順のまま、ParamSpec.group のまとまりへ分ける."""

    def test_groups_specs_in_the_backend_order(self):
        plate = ParamGroupInfo(label="銅板")
        detection = ParamGroupInfo(label="円検出", advanced=True)
        specs = [
            _spec("width", plate),
            _spec("min_area", detection),
            _spec("height", plate),
        ]

        sections = layout.param_sections(specs)

        assert [
            (section.label, section.advanced, [spec.name for spec in section.specs])
            for section in sections
        ] == [
            ("銅板", False, ["width", "height"]),
            ("円検出", True, ["min_area"]),
        ]

    def test_ungrouped_specs_form_one_unlabeled_section(self):
        sections = layout.param_sections([_spec("a", None), _spec("b", None)])

        assert [(section.label, len(section.specs)) for section in sections] == [
            (None, 2)
        ]

    def test_every_job_declares_each_group_contiguously(self, tmp_path: Path):
        """ジョブはまとまりごとに続けて宣言する（フォームの並びが宣言の並びと一致する）.

        飛び飛びに宣言すると、`param_sections` がまとめた時点で項目の並びが宣言と変わる。
        group の無い項目も 1 つのまとまり（無題）として扱う。
        """
        for definition in standalone_catalog(tmp_path).list():
            keys = [
                (spec.group.label, spec.group.advanced) if spec.group else None
                for spec in definition.params
            ]
            runs = [key for i, key in enumerate(keys) if i == 0 or keys[i - 1] != key]

            assert len(runs) == len(set(runs)), definition.name
