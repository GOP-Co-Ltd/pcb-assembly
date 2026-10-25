"""Frontend（`web.ui`）の SSR ページの仕様テスト.

ページを描くのは frontend で、値は backend の `GET /api/session/machine-info` / `/api/session/state` /
`/api/jobs` / `/api/machine_config` から取る。frontend は薄いラッパーなので、ここで押さえるのは次の 5 つに絞る:

- **URL 空間**: `/` の 307（自機の着地点か機体一覧）、未 prefix URL の 307、`/m/{machine_id}` prefix、
  旧 URL の 307、プロキシ経路が pages ルータに食われないこと、未知 feature の 404
- **backend が居ないとき**: 登録 0 台での案内、到達不能の 503、版ずれの 503、更新専用ページ
- **値を捏造しないこと**: 未記載キーは backend の `resolved` で描き、解決できないときは
  0 で埋めず 503 にする（スライダーの value がそのまま machine.toml へ書き戻るため）
- **machine.toml が壊れても SSR が 500 しない**（設定の現在値を要る
  `_MACHINE_TOML_DEPENDENT_URLS` だけが 503 に落ちる）
- **定義から導出される配線**: ジョブの ParamSpec がフォームに出る、preview /
  ローディング UI が `JobDefinition` と一致する、`setting_fields()` の全項目が設定フォームに出る

表示文字列・DOM id の網羅確認とページ上の実操作は `tests/e2e/` が実ブラウザで持つ。

実体の URL は `/m/{machine_id}/…` だが、`ui_settings` は同居機（frontend のホスト名 = machine_id）なので
未 prefix URL が自機へ 307 し、`follow_redirects` 既定 True の `TestClient` では prefix 無しのパスで叩ける。backend の
Settings を差し替える検証は、その backend を上流に挿した frontend を組む（`_frontend_over`）。
"""

import html
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import attrs
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcbasm.commissioning import STEPS
from pcbasm.config.fields import setting_fields
from pcbasm.machine.diagnostics import Check, save_checks
from tests.support.web import BACKEND_MACHINE_ID
from web.api.app import create_app as create_backend_app
from web.api.backend import Backend
from web.api.env import Env as ApiEnv
from web.api.jobs.runtime.catalog import JobCatalog
from web.ui.app import create_app as create_frontend_app
from web.ui.features import FEATURES, TABS, tab_features
from web.ui.machines import MachineEndpoint
from web.ui.settings import Settings as UiSettings

# SSR で描画される全ページ（/ は 307 なので除く）
ALL_PAGE_URLS = [f"/{feature.slug}" for feature in FEATURES]

# 設定フォームの現在値（ConfigStore / tomlkit）や paste_dispenser・alignment の
# 現在値をコンテキストに載せるページ。machine.toml がパース不能だと backend の
# `/api/machine_config` が 500 になり、frontend は 503 ページを返す（直す経路は設定履歴）
_MACHINE_TOML_DEPENDENT_URLS = frozenset(
    {
        "/machine_config",
        "/paste_solder",
        "/loading",
        "/nozzle_cap",
        "/copper_detection",
    }
)

# パース不能な machine.toml でも描画できるべきページ
ROBUST_PAGE_URLS = [
    url for url in ALL_PAGE_URLS if url not in _MACHINE_TOML_DEPENDENT_URLS
]

_OVERLAY_RADIO_RE = re.compile(r"<input[^>]*name=\"overlay\"[^>]*>")

# ジョブコンテキスト（job_name / param_specs）が注入される全ページ
JOB_PAGES = sorted(feature.slug for feature in FEATURES if feature.is_job)


def _loading_amount_input(default: object) -> str:
    """ローディング量入力（`#lc-amount`）の value を含む HTML 断片.

    同じ既定値の別 input が同一ページに 2〜4 箇所あるため、`value="..."` 単体では
    ローディング欄の既定値の注入が消えても通ってしまう（実測）。
    """
    return f'id="lc-amount" step="any" min="0" value="{default}"'


def _checked_overlay(html: str) -> str | None:
    """Overlay ラジオのうち checked が付いている value を返す（無ければ None）."""
    for tag in _OVERLAY_RADIO_RE.findall(html):
        if "checked" not in tag:
            continue
        value = re.search(r"value=\"([^\"]+)\"", tag)
        return value.group(1) if value else None
    return None


@pytest.fixture
def client(frontend_client: TestClient) -> TestClient:
    """UI frontend の TestClient.

    このファイルのページ取得はすべて frontend 経由（`web.ui`）で、上流には
    in-process の実 backend が挿さっている。移設前の `client` と同名にしてあるのは、
    テスト本文を変えずに移設できたことを diff で見えるようにするため。
    """
    return frontend_client


@pytest.fixture
def config_dir(backend_settings: ApiEnv) -> Path:
    """上流 backend が読む config ディレクトリ（machine.toml の書き換え材料）."""
    return backend_settings.config_dir


@pytest.fixture
def api_backend(backend_app: FastAPI) -> Backend:
    """上流 backend の Backend（保存済みジョブ既定値の注入に使う）."""
    return backend_app.state.backend


@pytest.fixture
def machine_toml_without_defaulted_keys(config_dir: Path) -> Path:
    """既定値を持つキーを machine.toml から削除する（未記載 → 既定値解決の素材）.

    実機の machine.toml はテンプレートから作るので、テンプレートに無いキーは普通に 未記載になる。未記載を frontend
    が 0 で埋めると、その 0 が画面に出たうえで 「設定に保存」で書き戻される。
    """
    path = config_dir / "machine.toml"
    dropped = (
        "canny_low",
        "canny_high",
        "blur_ksize",
        "refine_max_short_side",
        "solder_paste_density",
    )
    path.write_text(
        "".join(
            line
            for line in path.read_text(encoding="utf-8").splitlines(keepends=True)
            if not line.startswith(dropped)
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def machine_toml_without_required_paste_dispenser_key(config_dir: Path) -> Path:
    """`[paste_dispenser]` の必須キー（既定値なし）を 1 本落とす（そのパスを返す）.

    TOML としては読めるので backend の `/api/machine_config` は 200 を返すが、
    全体の検証に通らない（設定エラー = degraded）ので実効値（`resolved`）は全て None になる。
    テンプレートから作った machine.toml に必須キーが無い実機で普通に起きる状態。
    """
    path = config_dir / "machine.toml"
    path.write_text(
        "".join(
            line
            for line in path.read_text(encoding="utf-8").splitlines(keepends=True)
            if not line.startswith("nozzle_diameter")
        ),
        encoding="utf-8",
    )
    return path


@contextmanager
def _frontend_over(
    ui_settings: UiSettings,
    backend_settings: ApiEnv,
    *,
    catalog: JobCatalog | None = None,
) -> Iterator[TestClient]:
    """Settings を差し替えた backend を上流に挿した frontend の TestClient.

    ページを描くのは frontend なので、backend の Settings を変えた影響を
    ページで見るには、その backend を上流にした frontend を組む必要がある
    （conftest の `frontend_client` は既定の `backend_settings` 固定）。

    Args:
        ui_settings: frontend 設定（登録マシン 1 台）
        backend_settings: 上流 backend の設定
        catalog: backend のジョブカタログの差し替え（版ずれの再現に使う）
    """
    backend = create_backend_app(backend_settings)
    if catalog is not None:
        backend.state.backend = attrs.evolve(backend.state.backend, catalog=catalog)
    frontend = create_frontend_app(
        ui_settings,
        transport_factory=lambda _endpoint: httpx.ASGITransport(app=backend),
    )
    try:
        with TestClient(frontend) as test_client:
            yield test_client
    finally:
        # backend の lifespan は ASGITransport では走らないので明示的に後始末する
        backend.state.backend.close()


class TestPages:
    """Jinja2 ページ配信."""

    def test_root_opens_the_paste_page_of_this_host_machine(
        self, client: TestClient, ui_settings: UiSettings
    ):
        """同居機の `:8080` を開くと、クリック無しで自機の塗布ページに着く."""
        response = client.get("/", follow_redirects=False)

        assert response.status_code == 307
        machine_id = ui_settings.machines[0].machine_id
        assert response.headers["location"] == f"/m/{machine_id}/paste_solder"

    @pytest.mark.parametrize("tab", list(TABS))
    def test_tab_landing_renders_with_common_chrome(
        self, client: TestClient, backend_settings: ApiEnv, tab: str
    ):
        response = client.get(f"/{tab_features(tab)[0].slug}")

        assert response.status_code == 200
        assert "緊急停止" in response.text
        assert "再起動（復旧）" in response.text
        assert "machine-control" in response.text
        assert f'href="http://{BACKEND_MACHINE_ID}.local"' in response.text

    def test_file_browser_start_path_is_rendered(
        self, ui_settings: UiSettings, backend_settings: ApiEnv
    ):
        """Pcb_browse_start の pcb_browse_root 相対パスが data-fb-start に出る.

        値は backend が `/api/session/machine-info` の `fb_start` で解決するので、
        差し替えた backend Settings を上流に挿した frontend でページを取る。
        """
        evolved = attrs.evolve(
            backend_settings, pcb_browse_root=backend_settings.pcb_browse_root.parent
        )
        with _frontend_over(ui_settings, evolved) as client:
            text = client.get("/klipper_status").text

        start = backend_settings.pcb_browse_start.name
        assert f'data-fb-start="{start}"' in text

    def test_mainsail_link_resolves_from_machine_id_when_unset(
        self, ui_settings: UiSettings, backend_settings: ApiEnv
    ):
        """Mainsail のリンクは backend の machine_id から `.local` で解決する.

        `request.url.hostname` には頼らない。frontend を分離すると
        ページ閲覧元は frontend 機になるため、リクエストのホスト名を使うと必ず誤った
        Mainsail を指す。裸のホスト名だと LAN の他端末（mDNS しか引けない）から開けない
        ので `.local` を付ける。SSR も `/api/session/machine-info` と同じ解決結果を使う。

        frontend は backend が解決した値をそのまま描く（frontend 側に解決規則を
        持たない）。`/api/session/machine-info` はプロキシ経由で同じ値を返す。
        """
        settings = attrs.evolve(backend_settings, hostname="paste-01")
        with _frontend_over(ui_settings, settings) as client:
            text = client.get("/klipper_status").text
            info = client.get(
                f"/m/{BACKEND_MACHINE_ID}/api/session/machine-info"
            ).json()

        assert 'href="http://paste-01.local"' in text
        assert info["mainsail_url"] == "http://paste-01.local"
        assert "http://testserver" not in text

    def test_settings_page_renders_every_machine_field(self, client: TestClient):
        """`setting_fields()` の全キーが即保存フォームに出る.

        組の値（`reference_point.offsets.*` と座標）は複数の入力に分かれるので `data-pair-
        key` で出る。保存ボタンは持たない（変更した時点で PUT する）。
        """
        text = client.get("/machine_config").text

        missing = [
            field.key
            for field in setting_fields()
            if f'name="{field.key}"' not in text
            and f'data-pair-key="{field.key}"' not in text
        ]

        assert missing == []
        assert "single-pane" in text
        assert "settings-group" in text
        assert '<label for="ms-' not in text
        assert '<button type="submit">保存</button>' not in text

    def test_settings_page_renders_one_panel_per_section(self, client: TestClient):
        """左ナビと右パネルが 1 対 1 で、既定は先頭セクションだけを開く."""
        text = client.get("/machine_config").text

        nav_slugs = re.findall(
            r'class="settings-nav-item"[^>]*data-section="([^"]+)"', text
        )
        panel_slugs = re.findall(
            r'class="settings-section"[^>]*data-section="([^"]+)"', text
        )

        assert nav_slugs == list(dict.fromkeys(f.section for f in setting_fields()))
        assert panel_slugs == nav_slugs
        assert text.count('class="settings-section" data-section') == len(nav_slugs)

    def test_alignment_tolerance_is_in_the_alignment_panel(self, client: TestClient):
        """位置合わせ許容誤差はジョブのフォームではなく、設定ページの「位置合わせ」で変える."""
        text = client.get("/machine_config").text
        start = text.index('class="settings-section" data-section="alignment"')
        panel = text[start : text.index('class="settings-section"', start + 1)]

        assert '<h2 class="settings-section-title">位置合わせ</h2>' in panel
        assert "位置合わせ許容誤差" in panel
        assert 'name="alignment.tolerance"' in panel

    def test_settings_nav_and_filter_are_outside_the_control_gated_form(
        self, client: TestClient
    ):
        """操作権が無いと `data-requires-control` の中身は `inert` になる.

        ナビと絞り込みをフォームへ入れると、閲覧しかできない端末が設定を**読む**ことも
        できなくなる（`control.js` が form ごと inert にする）。
        """
        text = client.get("/machine_config").text
        form = text[text.index('<form id="machine-settings-form"') :]
        form = form[: form.index("</form>")]

        assert "data-requires-control" in form
        assert "settings-nav-item" not in form
        assert 'id="settings-filter"' not in form
        assert 'id="settings-filter"' in text

    def test_unknown_feature_returns_404(self, client: TestClient):
        assert client.get("/no-such-feature").status_code == 404

    def test_unknown_old_slug_returns_404(self, client: TestClient):
        assert client.get("/posctrl/no-such-feature").status_code == 404


class TestStaticAssets:
    """CSS / JS のブラウザキャッシュ対策."""

    def test_page_renders_versioned_static_asset_urls(self, client: TestClient):
        text = client.get("/paste_solder").text

        assert 'href="/static/app.css?v=' in text
        assert 'src="/static/js/app.js?v=' in text
        assert 'src="/static/js/job_console.js?v=' in text
        assert 'src="/static/js/pasting/pad_editor/index.js?v=' in text

    def test_static_assets_require_browser_revalidation(self, client: TestClient):
        response = client.get("/static/app.css")

        assert response.status_code == 200
        assert response.headers["cache-control"] == (
            "no-cache, max-age=0, must-revalidate"
        )


GENERIC_JOB_FEATURES = (
    "extract_pcb",
    "make_fill_coverage_pcb",
    "machine_check",
)


class TestGenericJobPages:
    """汎用のジョブページ（job.html。プレビューを持たない）."""

    @pytest.mark.parametrize("feature", GENERIC_JOB_FEATURES)
    def test_job_page_renders_job_console(self, client: TestClient, feature: str):
        response = client.get(f"/{feature}")

        assert response.status_code == 200
        assert "job-console" in response.text
        # data-job-name 等でページのジョブ名が宣言される
        assert feature in response.text


class TestConfigHistoryPage:
    """設定履歴とバックアップの画面（一覧と復元は JS が backend の API で行う）."""

    def test_backup_link_points_at_this_machine(self, client: TestClient):
        body = client.get("/config_history").text

        assert 'href="/m/uitest/api/backup"' in body
        assert 'id="config-history-list"' in body


ALIGN_JOB_FEATURES = ("camera_calibration", "board_tour", "orthogonality_test")


class TestAlignJobPages:
    """位置合わせ（align）のジョブページ（プレビューと十字線の切替を持つ）."""

    @pytest.mark.parametrize("feature", ALIGN_JOB_FEATURES)
    def test_align_job_page_renders_console_preview_and_overlay_switch(
        self, client: TestClient, feature: str
    ):
        response = client.get(f"/{feature}")

        assert response.status_code == 200
        assert "job-console" in response.text
        assert "preview-pane" in response.text
        assert "crosshair" in response.text  # overlay 切替（none / crosshair）
        # data-job-name 等でページのジョブ名が宣言される
        assert feature in response.text

    @pytest.mark.parametrize(
        ("feature", "expected"),
        (
            ("board_tour", "crosshair"),
            ("orthogonality_test", "crosshair"),
            ("camera_calibration", "none"),
            ("camera_preview", "none"),
        ),
    )
    def test_align_job_page_default_overlay(
        self, client: TestClient, feature: str, expected: str
    ):
        """ツアー系（board_tour / orthogonality_test）は十字線を既定 ON で描画する.

        ツアーは十字線を基準に位置を目視合わせするため、ユーザーがラジオを操作せずとも
        十字線が出ている必要がある。camera_calibration / camera_preview は素の映像を見る
        ページなので none。
        """
        assert _checked_overlay(client.get(f"/{feature}").text) == expected

    def test_camera_calibration_renders_square_size_form_with_default(
        self, client: TestClient
    ):
        """ジョブパラメータフォームは square_size のみ（初回既定値 1.5mm）。入力は次回の既定値として即保存する."""
        text = client.get("/camera_calibration").text

        assert 'name="square_size"' in text
        assert 'value="1.5"' in text
        assert "data-persist-params" in text
        # crop は job param から削除済み（machine.toml 連動の即保存フォームへ移設）
        assert "crop_width" not in text
        assert "crop_height" not in text

    def test_reference_point_setup_page_renders_record_and_quit(
        self, client: TestClient
    ):
        response = client.get("/reference_point_setup")

        assert response.status_code == 200
        text = response.text.lower()
        assert "job-console" in text
        assert "preview-pane" in text
        assert "record" in text
        assert "quit" in text
        assert "reference_point_setup.js" in text
        assert "設定へ即時反映" in response.text


PASTING_JOB_FEATURES = (
    "paste_solder",
    "height_plane",
    "loading",
    "dispense_calibration",
    "paste_volume_calibration",
    "generate_rect_pcb",
    "toolhead_offset",
)


class TestPastingJobPages:
    """Pasting のジョブページ.

    preview / ローディング UI の有無と段階は `TestJobDefinitionDrivenContext` が
    ジョブ定義から導出して検証するので、ここに残すのはページ固有の配線だけ。
    """

    PASTE_SOLDER_SETTING_KEYS = (
        "paste_dispenser.auto_line_aspect_ratio",
        "paste_dispenser.auto_area_short_side_factor",
        "paste_dispenser.volume_correction.calibration_file",
        "paste_dispenser.volume_correction.amount_ul",
        "paste_dispenser.volume_correction.crop_size_mm",
        "paste_dispenser.volume_correction.settle_seconds",
    )

    @pytest.mark.parametrize("setting_key", PASTE_SOLDER_SETTING_KEYS)
    def test_paste_solder_renders_the_machine_settings_form(
        self, client: TestClient, setting_key: str
    ):
        """Auto しきい値と流量キャリブ設定が machine 設定の即保存フォームに出る.

        操作の実体（保存・校正ファイルの選択・測定位置の指定）は e2e が実ブラウザで持つので、
        ここは「設定キーが即保存フォームに載っている」ことだけを見る。
        """
        text = client.get("/paste_solder").text

        assert "data-machine-settings" in text
        assert 'data-endpoint="/api/machine_config"' in text
        assert setting_key in text

    def test_paste_solder_renders_the_tact_estimate_slot(self, client: TestClient):
        """予測所要時間の枠は実行パネルに置き、値は JS が backend から取る."""
        text = client.get("/paste_solder").text

        assert 'data-testid="tact-estimate-label"' in text
        assert "js/pasting/tact_estimate.js" in text

    def test_loading_page_renders_optional_start_position(self, client: TestClient):
        text = client.get("/loading").text

        assert "実行すると全軸をホーミング" in text
        assert "ローディング位置（任意）" in text
        assert "空欄の軸はホーミング後の位置を維持します" in text
        for axis in ("x", "y", "z"):
            assert f'id="param-position_{axis}"' in text
        assert text.count('data-param-optional="true"') == 3

    def test_loading_page_wires_the_mass_calibration_table(self, client: TestClient):
        """質量キャリブ表はローディング画面に残す（`data-job-names` で対象ジョブを宣言）.

        既存値を線引きで補正する dispense_calibration とは用途が別（初期値をゼロから
        決めるブートストラップ用）なので併存させる。表の操作は e2e が持つ。
        """
        text = client.get("/loading").text

        assert 'id="loading-mass-calibration"' in text
        assert 'data-job-names="loading"' in text

    def test_dispense_calibration_renders_the_calibration_menu(
        self, client: TestClient
    ):
        """①②③/全実行/終了メニューと、フォームのセクション分けが出る."""
        text = client.get("/dispense_calibration").text

        assert "job-param-group" in text
        for legend in (
            "銅板（キャリブ後固定）",
            "線の共通設定（実行中変更可）",
            "② max_dispense_rate",
            "③ max_fill_speed",
        ):
            assert legend in text
        assert "calibration-menu" in text
        for bid in (
            "calib-rotations-per-ul",
            "calib-max-dispense-rate",
            "calib-max-fill-speed",
            "calib-all",
            "calib-finish",
        ):
            assert f'id="{bid}"' in text
        assert "calibration_menu.js" in text

    def test_advanced_param_group_is_collapsed_by_default(self, client: TestClient):
        """ParamSpec.group が advanced のまとまり（円検出のハイパラ）は既定で畳んで描く."""
        text = client.get("/paste_volume_calibration").text

        advanced = re.search(
            r'<details class="job-param-advanced"([^>]*)>(.*?)</details>', text, re.S
        )
        assert advanced is not None
        assert "open" not in advanced.group(1)
        assert "<summary>円検出のハイパラ</summary>" in advanced.group(2)
        assert 'id="param-min_contrast"' in advanced.group(2)
        assert 'id="param-plate_width"' not in advanced.group(2)
        assert "<legend>銅板</legend>" in text


class TestJobParamForms:
    """ジョブの ParamSpec がフォームへ届く（`ParamSpec` を足したのに UI に出ない事故の検出）."""

    @pytest.mark.parametrize("feature", JOB_PAGES)
    def test_every_job_param_has_an_input(
        self, client: TestClient, backend_app: FastAPI, feature: str
    ):
        definition = backend_app.state.backend.catalog.get(feature)
        assert definition is not None, feature

        text = client.get(f"/{feature}").text

        missing = [
            spec.name
            for spec in definition.params
            if f'id="param-{spec.name}"' not in text
        ]
        assert missing == [], feature


class TestSavedJobParamDefaults:
    """入力途中の即保存値（param-defaults）が次回描画の default に反映される.

    ページごとに別テンプレートを使うが、`api_backend.selection.merge_job_param_defaults` した値が
    `value="..."` に出るという同じ機構で動く。
    """

    @pytest.mark.parametrize(
        ("url", "job_name", "defaults", "fragments"),
        (
            pytest.param(
                "/camera_calibration",
                "camera_calibration",
                {"square_size": 3.0},
                ('value="3.0"',),
                id="camera_calibration",
            ),
            pytest.param(
                "/loading",
                "loading",
                {"amount": 0.2, "rotations": 6.0, "rate": 1.5, "accel": 2.5},
                (
                    _loading_amount_input(0.2),
                    'value="6.0"',
                    'value="1.5"',
                    'value="2.5"',
                ),
                id="loading",
            ),
            pytest.param(
                "/dispense_calibration",
                "dispense_calibration",
                {"board_width": 30.0, "line_count": 8, "speed_max": 12.0},
                ('value="30.0"', 'value="8"', 'value="12.0"'),
                id="dispense_calibration",
            ),
            pytest.param(
                "/toolhead_offset",
                "toolhead_offset",
                {"point_count": 7, "point_spacing": 6.25, "edge_margin": 4.75},
                ('value="7"', 'value="6.25"', 'value="4.75"'),
                id="toolhead_offset",
            ),
        ),
    )
    def test_saved_defaults_are_rendered_as_input_values(
        self,
        client: TestClient,
        api_backend: Backend,
        url: str,
        job_name: str,
        defaults: dict[str, float],
        fragments: tuple[str, ...],
    ):
        api_backend.selection.merge_job_param_defaults(job_name, defaults)

        text = client.get(url).text

        for fragment in fragments:
            assert fragment in text, fragment


class TestJobDefinitionDrivenContext:
    """Preview / ローディング UI は JobDefinition から導出する.

    preview フレームの提供有無と progress_stage 文字列は backend の事実なのでジョブ定義が持ち、
    ページはそれを読むだけ（`/api/jobs` と同じ値になる）。
    """

    @pytest.mark.parametrize("feature", PASTING_JOB_FEATURES)
    def test_preview_pane_presence_matches_provides_preview(
        self, client: TestClient, backend_app: FastAPI, feature: str
    ):
        definition = backend_app.state.backend.catalog.get(feature)

        text = client.get(f"/{feature}").text

        assert ("preview-pane" in text) is definition.provides_preview
        # pasting の preview は固定 none（overlay 切替は align のページだけ）
        assert "crosshair" not in text

    @pytest.mark.parametrize("feature", PASTING_JOB_FEATURES)
    def test_loading_controls_derive_stage_and_default_from_definition(
        self, client: TestClient, backend_app: FastAPI, feature: str
    ):
        """Loading UI の段階と既定値が定義側の値と一致する.

        `loading.html` / `dispense_calibration.html` は loading_controls を
        ローディング欄の有無で gate せず無条件 include するため、この 2 feature では
        `"loading-controls" in text` が定数 True になる（検出力ゼロ）。段階と
        `#lc-amount` の value を見て、全 feature で定義との一致を確かめる。
        """
        definition = backend_app.state.backend.catalog.get(feature)

        text = client.get(f"/{feature}").text

        panel = definition.loading
        if panel is None:
            assert "loading-controls" not in text
            return
        stages = re.search(r'data-loading-stages="([^"]*)"', text)
        assert stages is not None
        assert json.loads(html.unescape(stages.group(1))) == list(panel.stages)
        default = next(
            spec.default for spec in definition.params if spec.name == panel.amount
        )
        assert _loading_amount_input(default) in text


class TestProbeGuidePage:
    """ロードセルプローブのガイドページ.

    ジョブではない静的な説明ページ。旧サーボ方式の probe_gnd_down_adjust ジョブは削除済みで、タブ・URL
    のどちらからも到達できない。
    """

    def test_probe_guide_page_renders_calibration_steps(self, client: TestClient):
        response = client.get("/probe_guide")

        assert response.status_code == 200
        # 較正手順（Klipper コンソールで実行するコマンド）と公式ドキュメントリンク
        assert "LOAD_CELL_CALIBRATE" in response.text
        assert "SAVE_CONFIG" in response.text
        assert "klipper3d.org" in response.text


class TestNozzleCapPage:
    """ノズルキャップ位置の設定ページ.

    ジョブではない静的な設定ページ（非ジョブテンプレート）。現在値が未記録 （repo fixture に
    [paste_dispenser.nozzle_cap] なし）のときは「未記録」を表示する。
    """

    def test_partially_recorded_cap_shows_placeholder(
        self, partial_nozzle_cap: Path, client: TestClient
    ):
        """X だけ手で書いた状態（設定エラー）でも 500 にせず、「未記録」と理由を出す."""
        response = client.get("/nozzle_cap")

        assert response.status_code == 200
        assert "未記録" in response.text
        assert 'data-testid="config-error"' in response.text

    def test_shows_cleaning_controls_and_settings(self, client: TestClient):
        """記録・試運転・設定変更を同じページで行える（座標は記録ボタンから保存する）."""
        response = client.get("/nozzle_cap")

        assert response.status_code == 200
        assert 'data-testid="nozzle-clean-current"' in response.text
        assert 'data-testid="nozzle-clean-record"' in response.text
        assert 'data-testid="nozzle-clean-test"' in response.text
        # 試運転はジョブなので、同じページで経過を見て中止できる
        assert 'data-job-name="nozzle_clean_test"' in response.text
        assert 'data-job-names="nozzle_clean_test"' in response.text
        assert 'data-testid="nozzle-clean-settings"' in response.text
        assert 'name="paste_dispenser.nozzle_clean.press_depth"' in response.text
        # 座標は記録ボタンの管轄なので手打ち欄を並べない
        assert "paste_dispenser.nozzle_clean.position" not in response.text

    def test_partially_recorded_clean_shows_placeholder(
        self, partial_nozzle_clean: Path, client: TestClient
    ):
        """座標の無い `[paste_dispenser.nozzle_clean]` でも 500 にせず「未記録」を出す."""
        response = client.get("/nozzle_cap")

        assert response.status_code == 200
        assert "未記録" in response.text

    def test_shows_recorded_clean_label_from_server(
        self, config_dir: Path, client: TestClient
    ):
        """表示文字列はサーバーが組んだ label をそのまま出す."""
        path = config_dir / "machine.toml"
        with path.open("a", encoding="utf-8") as machine_toml:
            machine_toml.write(
                "\n[paste_dispenser.nozzle_clean]\nx = 10.0\ny = 20.0\nz = -30.0\npress_depth = 0.4\n"
            )

        response = client.get("/nozzle_cap")

        # 同じページの設定ラベルに当たらないよう、label 文字列そのものを見る
        assert "(10.00, 20.00, -30.00) mm" in response.text

    def test_shows_recorded_cap_label_from_server(
        self, config_dir: Path, client: TestClient
    ):
        """キャップ位置も、サーバーが組んだ label をそのまま出す（テンプレートで書式を組まない）."""
        path = config_dir / "machine.toml"
        with path.open("a", encoding="utf-8") as machine_toml:
            machine_toml.write(
                "\n[paste_dispenser.nozzle_cap]\nx = 10.0\ny = 20.0\nz = -3.5\n"
            )

        response = client.get("/nozzle_cap")

        assert "X10.000 Y20.000 Z-3.500" in response.text


class TestCommissioningPage:
    """立ち上げチェックリスト（校正タブの入口）とトップバーの立ち上げチップ.

    段の並び・状態・文言は backend の ``/api/readiness`` が組み、frontend はそのまま描く。
    """

    @pytest.fixture
    def commissioned(self, config_dir: Path, backend_settings: ApiEnv) -> None:
        """全段を済ませた機体（ノズル位置・運転時補正の校正ファイル・合格した装置診断）."""
        with (config_dir / "machine.toml").open("a", encoding="utf-8") as machine_toml:
            machine_toml.write(
                "\n[paste_dispenser.nozzle_cap]\nx = 1.0\ny = 2.0\nz = 3.0\n"
                "\n[paste_dispenser.nozzle_clean]\nx = 4.0\ny = 5.0\nz = 6.0\n"
                '\n[paste_dispenser.volume_correction]\ncalibration_file = "a.json"\n'
            )
        calibration_dir = backend_settings.paste_volume_calibration_dir
        calibration_dir.mkdir(parents=True)
        (calibration_dir / "a.json").write_text("{}")
        save_checks(backend_settings.machine_check_file, [Check("接続", True, "ok")])

    def test_lists_each_step_with_a_link_in_order(self, client: TestClient):
        text = client.get("/commissioning").text

        positions = [
            text.index(f'data-testid="commissioning-step-{step.name}"')
            for step in STEPS
        ]
        assert positions == sorted(positions)
        for step in STEPS:
            assert f'href="/m/{BACKEND_MACHINE_ID}/{step.name}"' in text
        assert "まだ診断していません" in text
        assert "先に「装置診断」を済ませてください" in text

    def test_chip_opens_the_checklist_while_steps_remain(self, client: TestClient):
        text = client.get("/paste_solder").text

        chip = re.search(r'<a [^>]*data-testid="readiness-chip">([^<]*)</a>', text)
        assert chip is not None
        assert f'href="/m/{BACKEND_MACHINE_ID}/commissioning"' in chip.group(0)
        assert chip.group(1) == "立ち上げ 4/7"

    def test_chip_is_gone_once_every_step_is_done(
        self, commissioned: None, client: TestClient
    ):
        assert 'data-testid="readiness-chip"' not in client.get("/paste_solder").text
        checklist = client.get("/commissioning").text
        assert checklist.count('data-state="done"') == len(STEPS)


class TestMissingSelectionNotice:
    """起動時に選択していた PCB が無ければ、選択を解除したことを全ページに出す."""

    @pytest.fixture
    def missing_selection(self, backend_settings: ApiEnv) -> None:
        """Backend 起動前に、もう無い PCB を選択済みとして残す."""
        state_file = backend_settings.webui_data_dir / "webui_state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            json.dumps({"pcb_file": "boards/gone.kicad_pcb"}), encoding="utf-8"
        )

    def test_notice_is_shown_until_a_pcb_is_selected(
        self, missing_selection: None, client: TestClient
    ):
        page = client.get("/klipper_status").text
        assert 'data-testid="pcb-notice"' in page
        assert "boards/gone.kicad_pcb" in page

        response = client.put(
            f"/m/{BACKEND_MACHINE_ID}/api/selection/pcb-file",
            json={"path": "boards/sample.kicad_pcb"},
        )
        assert response.status_code == 200, response.text

        assert 'data-testid="pcb-notice"' not in client.get("/klipper_status").text


class TestBrokenMachineTomlPages:
    """パース不能な machine.toml でも SSR が落ちない（設定エラー = degraded）.

    全ページが読む `/api/session/state` と `/api/session/machine-info` は、設定エラーの間も 200 で理由を返す。
    設定フォームの現在値をコンテキストに載せるページ（`_MACHINE_TOML_DEPENDENT_URLS`）は
    TOML を読めないと描けないので 503 になる（直す経路は設定履歴の復元）。
    """

    @pytest.mark.parametrize("url", ROBUST_PAGE_URLS)
    def test_broken_machine_toml_keeps_pages_renderable(
        self, broken_machine_toml: Path, client: TestClient, url: str
    ):
        response = client.get(url)

        assert response.status_code == 200
        assert 'data-testid="config-error"' in response.text

    def test_error_banner_disappears_once_fixed_without_restart(
        self, config_dir: Path, client: TestClient
    ):
        """手編集（ssh）で壊して直すと、再起動なしで次のページ表示から理由が現れて消える."""
        path = config_dir / "machine.toml"
        original = path.read_text(encoding="utf-8")
        path.write_text(original + '\nbroken_key = "unterminated\n', encoding="utf-8")

        assert 'data-testid="config-error"' in client.get("/klipper_status").text

        path.write_text(original, encoding="utf-8")

        assert 'data-testid="config-error"' not in client.get("/klipper_status").text

    @pytest.mark.parametrize("url", sorted(_MACHINE_TOML_DEPENDENT_URLS))
    def test_machine_toml_dependent_pages_return_503(
        self, broken_machine_toml: Path, client: TestClient, url: str
    ):
        """設定の現在値を要るページは 500 ではなく 503 を返す.

        frontend が全ページで `/api/machine_config` を取ると、machine.toml が
        壊れただけで案内も出せない 503 が全画面に出る。取得を必要なページに限って
        いることの裏返しとして、必要なページだけがここに落ちる。
        """
        assert client.get(url).status_code == 503


class TestUnsetMachineSettingsShowResolvedValues:
    """machine.toml に未記載のキーは backend が解決した実効値で描く.

    frontend が「未記載 → 0.0」で埋めると、copper_detection は canny 0 / blur_ksize 0 を
    描き、その 0 が「設定に保存」で machine.toml へ書き戻されて銅箔検出が壊れる
    （loading は密度 0.000 mg/uL を出す）。既定値の出所は backend の `resolved` だけに
    保ち、frontend は既定値を持たない。
    """

    def test_copper_detection_renders_resolved_canny_values(
        self, machine_toml_without_defaulted_keys: Path, client: TestClient
    ):
        text = client.get("/copper_detection").text

        assert "canny" in text
        assert "設定に保存" in text
        assert 'id="canny-low"' in text
        assert 'id="canny-high"' in text
        assert "sharpen-amount" not in text
        # PadAlign の既定値（canny_low=100 / canny_high=200 / blur_ksize=5）
        assert "blur_ksize: 5" in text
        assert '<span id="canny-low-value" class="canny-value">100</span>' in text
        assert '<span id="canny-high-value" class="canny-value">200</span>' in text
        # スライダーの value がそのまま PUT されるので、捏造した 0 を載せない
        assert '<span id="canny-low-value" class="canny-value">0</span>' not in text
        assert "blur_ksize: 0" not in text

    def test_nozzle_clean_form_renders_resolved_values(
        self, config_dir: Path, client: TestClient
    ):
        """位置だけ記録した状態でも、動作設定は既定値を placeholder に描く.

        記録直後がこの状態なので、「未設定」と出すと既定値で動いているのにそう見えない。
        値には入れない（未記載と明示した値を見分けられるようにする）。既定値の出所は backend の `resolved` だけに保つ。
        """
        path = config_dir / "machine.toml"
        with path.open("a", encoding="utf-8") as machine_toml:
            machine_toml.write(
                "\n[paste_dispenser.nozzle_clean]\nx = 10.0\ny = 20.0\nz = -30.0\n"
            )

        text = client.get("/nozzle_cap").text

        # NozzleClean の既定値（press_depth=0.5 / stroke=2.0 / passes=2）
        assert 'name="paste_dispenser.nozzle_clean.press_depth"' in text
        assert 'placeholder="0.5"' in text
        assert 'placeholder="2.0"' in text
        assert 'placeholder="2"' in text

    def test_loading_renders_resolved_paste_density(
        self, machine_toml_without_defaulted_keys: Path, client: TestClient
    ):
        text = client.get("/loading").text

        # PasteDispenser.solder_paste_density の既定値
        assert '<dd>3.780 <span class="unit">mg/uL</span></dd>' in text
        assert '<dd>0.000 <span class="unit">mg/uL</span></dd>' not in text
        # machine.toml に書かれているキーは書かれている値のまま
        assert ">45.783133</output>" in text

    def test_paste_solder_renders_resolved_refinement_threshold(
        self, machine_toml_without_defaulted_keys: Path, client: TestClient
    ):
        response = client.get("/paste_solder")

        assert response.status_code == 200
        assert "逐次位置合わせ対象の最大短辺" in response.text
        input_tag = re.search(
            r'<input[^>]*name="alignment\.refine_max_short_side"[^>]*>',
            response.text,
        )
        assert input_tag is not None
        assert 'type="number"' in input_tag.group()
        assert 'value=""' in input_tag.group()
        assert 'placeholder="0.4"' in input_tag.group()
        assert '<span class="unit">mm</span>' in response.text


class TestUnresolvableMachineSettingsAreNotFabricated:
    """実効値が解決できないときは 0 を捏造せず 503 を返す.

    backend が落ちているわけではない（`/api/machine_config` は 200）ので、frontend が
    `resolved: None` を 0.0 で埋めると copper_detection は canny 0 / blur_ksize
    0 を描き、 その 0 がスライダーの value として「設定に保存」で実機の machine.toml へ書き戻される。
    実効値の出所は backend だけに保ち、解決できないなら描かない。
    """

    def test_settings_api_answers_200_with_unresolved_values(
        self,
        machine_toml_without_required_paste_dispenser_key: Path,
        client: TestClient,
        ui_settings: UiSettings,
    ):
        """Backend は 200 を返し、失うのは実効値だけ（生値は tomlkit で読めるまま）.

        frontend が 503 にするのは「backend が落ちているから」ではなく
        「実効値が無いから」であることを、上流の応答側から示す。設定エラーの間は、
        部分的に解決した実効値を混ぜずに全項目の実効値を出さない。
        """
        machine_id = ui_settings.machines[0].machine_id

        response = client.get(f"/m/{machine_id}/api/machine_config")

        assert response.status_code == 200
        fields = {field["key"]: field for field in response.json()["fields"]}
        assert fields["alignment.canny_low"]["resolved"] is None
        assert fields["probe.min_samples"]["resolved"] is None
        assert fields["paste_dispenser.rotations_per_ul"]["value"] == 45.783133

    def test_copper_detection_returns_503_instead_of_zero(
        self,
        machine_toml_without_required_paste_dispenser_key: Path,
        client: TestClient,
    ):
        response = client.get("/copper_detection")

        assert response.status_code == 503
        # 0 を描いたページを見せない（スライダーの value がそのまま PUT される）
        assert "canny-low-value" not in response.text


class TestMachineControlCapButton:
    """マシン操作パネルの「キャップ位置へ」ボタン（全機体がペースト機なので常に出す）."""

    def test_move_to_cap_button_is_shown(self, client: TestClient):
        response = client.get("/klipper_status")

        assert response.status_code == 200
        assert "mc-move-to-cap" in response.text


class TestPasteVolumeCalibrationLoading:
    """塗布量校正の生成ページのローディング操作 UI.

    pad editor の DOM フックと表示要素は e2e が実ブラウザで操作する。
    """

    def test_volume_and_rotation_loading_derive_from_the_job_definition(
        self, client: TestClient, backend_app: FastAPI
    ):
        """塗布パス先頭のローディングで体積・回転の両方を操作でき、既定値はフォームの値になる."""
        definition = backend_app.state.backend.catalog.get("paste_volume_calibration")
        defaults = {spec.name: spec.default for spec in definition.params}
        assert definition.loading is not None
        rotation = definition.loading.rotation
        assert rotation is not None

        text = client.get("/paste_volume_calibration").text

        assert 'id="lc-amount"' in text
        for element, name in (
            ("lc-rotations", rotation.rotations),
            ("lc-rate", rotation.rate),
            ("lc-accel", rotation.accel),
            ("lc-retract-rotations", rotation.retract_rotations),
        ):
            assert re.search(
                rf'id="{element}"[^>]*value="{defaults[name]}"', text
            ), element


def _frontend_only(machines: tuple[MachineEndpoint, ...], tmp_path: Path) -> TestClient:
    """Backend を持たない frontend の TestClient.

    レジストリだけで描けるページ（ピッカー・未登録の案内・到達不能の 503）用。
    `machines_file` は不在パスにしてリポジトリの `config/machines.toml` を拾わない。
    """
    return TestClient(
        create_frontend_app(
            UiSettings(
                machines=machines,
                machines_file=tmp_path / "absent.toml",
                discovery_enabled=False,
            )
        )
    )


class TestMachinePrefixedUrls:
    """`/m/{machine_id}` prefix の URL 空間.

    未 prefix の URL は入口（307）で、実体は prefix 付き。prefix が落ちると ブラウザは同居 backend
    や別マシンを叩いてしまうため、リンク・フォーム・ ドロップダウンの遷移先すべてに prefix が乗っている必要がある。
    """

    def test_unprefixed_feature_url_redirects_to_this_host_machine(
        self, client: TestClient, ui_settings: UiSettings
    ):
        machine_id = ui_settings.machines[0].machine_id

        response = client.get("/loading", follow_redirects=False)

        assert response.status_code == 307
        assert response.headers["location"] == f"/m/{machine_id}/loading"

    def test_every_navigation_link_is_machine_prefixed(
        self, client: TestClient, ui_settings: UiSettings
    ):
        """タブ・サイドバー・設定リンクと JS の base が prefix 付きになる.

        `app.js` は `data-machine-base` を全 `fetch` の prefix に使うので、この属性が
        落ちると frontend 自身に `/api/...` を投げて 404 になる。
        """
        machine_id = ui_settings.machines[0].machine_id
        base = f"/m/{machine_id}"

        text = client.get(f"{base}/loading").text

        assert f'data-machine-base="{base}"' in text
        assert f'href="{base}/commissioning"' in text
        assert f'href="{base}/machine_config"' in text
        assert f'href="{base}/height_plane"' in text
        # 未 prefix の内部リンクが 1 本も残っていない（外部リンクは mainsail のみ）。
        # /static は意図的に prefix しない（キャッシュを全マシンで 1 本共有する）。
        # 機体一覧もマシン非依存のページなので prefix しない
        internal = [
            href
            for href in re.findall(r'href="(/[^"]*)"', text)
            if not href.startswith("/static/") and href != "/machines"
        ]
        assert internal
        assert [href for href in internal if not href.startswith(f"{base}/")] == []

    def test_api_path_is_proxied_and_not_eaten_by_the_pages_router(
        self, client: TestClient, ui_settings: UiSettings
    ):
        """`/m/{id}/api/session/state` はプロキシに届く（登録順の回帰）.

        pages ルータを先に登録すると、キャッチオールがこの URL を飲み込み、UI 全体が動かなくなる。
        """
        machine_id = ui_settings.machines[0].machine_id

        response = client.get(f"/m/{machine_id}/api/session/state")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert "busy" in response.json()

    def test_artifacts_path_is_proxied_and_not_eaten_by_the_pages_router(
        self, client: TestClient, ui_settings: UiSettings, backend_settings: ApiEnv
    ):
        """`/m/{id}/artifacts/...` も同じ（成果物リンクとジョブの画像表示）."""
        backend_settings.webui_data_dir.mkdir(parents=True, exist_ok=True)
        (backend_settings.webui_data_dir / "probe.txt").write_text("ok", "utf-8")
        machine_id = ui_settings.machines[0].machine_id

        response = client.get(f"/m/{machine_id}/artifacts/probe.txt")

        assert response.status_code == 200
        assert response.text == "ok"

    def test_machine_selector_keeps_the_current_page_when_switching(
        self, client: TestClient, ui_settings: UiSettings
    ):
        """ドロップダウンの遷移先は「同じページの別マシン」."""
        machine = ui_settings.machines[0]

        text = client.get(f"/m/{machine.machine_id}/camera_preview").text

        assert f'value="/m/{machine.machine_id}/camera_preview"' in text
        assert machine.label in text

    def test_machine_id_alone_redirects_to_the_paste_page(
        self, client: TestClient, ui_settings: UiSettings
    ):
        """`/m/{id}` 単体は着地点（塗布）へ 307 する."""
        machine_id = ui_settings.machines[0].machine_id

        response = client.get(f"/m/{machine_id}", follow_redirects=False)

        assert response.status_code == 307
        assert response.headers["location"] == f"/m/{machine_id}/paste_solder"

    def test_unknown_machine_returns_404_page_with_the_selector(
        self, client: TestClient, ui_settings: UiSettings
    ):
        """未知の machine_id は 404 HTML（ドロップダウンと機体一覧から戻れる）."""
        response = client.get("/m/no-such-machine/klipper_status")

        assert response.status_code == 404
        assert response.headers["content-type"].startswith("text/html")
        assert ui_settings.machines[0].label in response.text
        # 未知のマシンを base にするとタブが全部 404 になるので、作業分野のタブは出さない
        assert 'href="/machines"' in response.text
        assert "/m/no-such-machine/paste_solder" not in response.text
        assert 'data-testid="estop"' not in response.text


class TestLegacyUrls:
    """旧 URL（作り直し前のブックマーク）は 1 規則の 307 で新しい URL へ飛ぶ."""

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("/m/uitest/pasting/paste_solder", "/m/uitest/paste_solder"),
            ("/m/uitest/posctrl/camera_preview", "/m/uitest/camera_preview"),
            ("/m/uitest/dev/klipper_status", "/m/uitest/klipper_status"),
            ("/m/uitest/settings", "/m/uitest/machine_config"),
            ("/m/uitest/dev/update", "/update"),
            ("/m/uitest/posctrl", "/m/uitest/camera_calibration"),
            ("/m/uitest/pasting", "/m/uitest/paste_solder"),
            ("/m/uitest/dev", "/m/uitest/klipper_status"),
            ("/bulk", "/machines"),
        ],
    )
    def test_old_url_redirects_to_the_new_page(
        self, client: TestClient, old: str, new: str
    ):
        response = client.get(old, follow_redirects=False)

        assert response.status_code == 307
        assert response.headers["location"] == new

    @pytest.mark.parametrize(
        "path", ["/m/uitest/pasting/no-such-feature", "/m/uitest/no-such-tab/loading"]
    )
    def test_unknown_old_url_returns_404(self, client: TestClient, path: str):
        assert client.get(path).status_code == 404

    def test_deep_unknown_machine_url_returns_404_instead_of_looping(
        self, client: TestClient
    ):
        """/m/ の下で route に当たらない URL を、マシン非依存の入口として自機へ飛ばさない."""
        response = client.get("/m/uitest/a/b/c", follow_redirects=False)

        assert response.status_code == 404


class TestWithoutMachines:
    """マシン登録 0 台（machine.toml が無いホストでの単独起動）."""

    def test_create_app_succeeds_and_serves_guidance(self, tmp_path: Path):
        """機体設定を一切読まずに起動でき、機体一覧で登録の仕方を案内する.

        frontend が machine.toml に触ると、機体でない PC で起動できなくなる。
        """
        with _frontend_only((), tmp_path) as client:
            response = client.get("/")

        assert response.status_code == 200
        assert str(response.url).endswith("/machines")
        assert "machines.toml" in response.text
        # 選ぶ先が無いのでドロップダウンは出さない
        assert 'data-testid="machine-select"' not in response.text
        assert 'data-testid="estop"' not in response.text


class TestDefaultBackendPort:
    """Port を省略した `machines.toml` の登録には `Settings.default_backend_port` を使う.

    `load_machines_file` が module 定数を直参照すると、host しか判らないマシンが
    「設定したのに 8081 へ繋ぐ」形で表面化する。
    """

    def test_default_port_applies_to_entries_without_a_port(self, tmp_path: Path):
        machines_file = tmp_path / "machines.toml"
        machines_file.write_text(
            '[[machine]]\nmachine_id = "alpha"\nhost = "127.0.0.1"\n', encoding="utf-8"
        )
        settings = UiSettings(
            machines_file=machines_file,
            default_backend_port=19999,
            discovery_enabled=False,
            update_enabled=False,
        )

        with TestClient(create_frontend_app(settings)) as client:
            response = client.get("/m/alpha/klipper_status")

        # 誰も listen していない port なので 503。到達を試みた URL が本文に出る
        assert response.status_code == 503
        assert "http://127.0.0.1:19999" in response.text


class TestMultipleMachines:
    """マシンが複数登録されているとき（どれを開くかは選ばせる）."""

    @staticmethod
    def _machines() -> tuple[MachineEndpoint, ...]:
        return (
            MachineEndpoint(machine_id="alpha", host="alpha.local", port=8081),
            MachineEndpoint(machine_id="bravo", host="bravo.local", port=8081),
        )

    def test_root_opens_the_machine_list_when_no_machine_is_this_host(
        self, tmp_path: Path
    ):
        """UI サーバー専用機（自機の無い frontend）の `/` は機体一覧へ飛ぶ."""
        with _frontend_only(self._machines(), tmp_path) as client:
            response = client.get("/", follow_redirects=False)
            listing = client.get(response.headers["location"])

        assert response.status_code == 307
        assert response.headers["location"] == "/machines"
        for machine in self._machines():
            assert machine.label in listing.text
        assert 'data-testid="estop"' not in listing.text

    def test_root_opens_this_host_machine_among_several(self, tmp_path: Path):
        """同居機は、ほかの機体が登録されていても自機の塗布ページに着く."""
        settings = UiSettings(
            machines=self._machines(),
            machines_file=tmp_path / "absent.toml",
            discovery_enabled=False,
            hostname="bravo",
        )
        with TestClient(create_frontend_app(settings)) as client:
            response = client.get("/", follow_redirects=False)

        assert response.status_code == 307
        assert response.headers["location"] == "/m/bravo/paste_solder"

    def test_machine_list_selector_opens_the_paste_page(self, tmp_path: Path):
        """マシン非依存のページで選んだ機体は、着地点（塗布）を開く."""
        with _frontend_only(self._machines(), tmp_path) as client:
            text = client.get("/machines").text

        assert 'value="/m/alpha/paste_solder"' in text
        assert 'value="/m/bravo/paste_solder"' in text


class TestUnreachableBackendPage:
    """Backend に到達できないときのページ（`BackendUnavailable` → 503）.

    上流は実ソケット（誰も listen していない port）。欠損値のフォームを描くより
    503 を出す方針（`MachineClient` の契約）が SSR ページでも守られていること。
    """

    @staticmethod
    def _dead() -> tuple[MachineEndpoint, ...]:
        return (MachineEndpoint(machine_id="dead", host="127.0.0.1", port=1),)

    def test_page_returns_503_html_with_the_machine_selector(self, tmp_path: Path):
        with _frontend_only(self._dead(), tmp_path) as client:
            response = client.get("/m/dead/klipper_status")

        assert response.status_code == 503
        assert response.headers["content-type"].startswith("text/html")
        # ドロップダウンは frontend の登録一覧から描くので backend 不要 = 必ず出る
        assert 'data-testid="machine-select"' in response.text
        assert self._dead()[0].label in response.text

    def test_page_asks_the_browser_to_retry(self, tmp_path: Path):
        with _frontend_only(self._dead(), tmp_path) as client:
            response = client.get("/m/dead/klipper_status")

        assert response.headers["retry-after"] == "5"


class TestUpdateOnlyMachinePage:
    """API 版のずれた機体のページ（どの URL も更新専用ページになる）.

    proxy はこの機体へ更新と緊急停止だけを中継するので、ページは backend に問い合わせずに描き、
    緊急停止と更新ページへの案内だけを出す。上流は誰も listen していない port で、到達を試みれば 503 になる。
    """

    @staticmethod
    def _old() -> tuple[MachineEndpoint, ...]:
        return (
            MachineEndpoint(
                machine_id="old", host="127.0.0.1", port=1, update_only=True
            ),
        )

    @pytest.mark.parametrize(
        "path", ["/m/old/paste_solder", "/m/old/machine_config", "/m/old/settings"]
    )
    def test_every_machine_page_leads_to_the_update_page(
        self, tmp_path: Path, path: str
    ):
        with _frontend_only(self._old(), tmp_path) as client:
            response = client.get(path)

        assert response.status_code == 200
        assert 'data-machine-base="/m/old"' in response.text
        assert 'data-testid="update-only-detail"' in response.text
        assert 'href="/update"' in response.text

    def test_keeps_the_emergency_stop_but_no_unrelayed_commands(self, tmp_path: Path):
        with _frontend_only(self._old(), tmp_path) as client:
            text = client.get("/m/old/paste_solder").text

        assert 'data-testid="estop"' in text
        assert 'data-testid="topbar-menu"' not in text

    def test_has_no_tabs_that_lead_back_to_itself(self, tmp_path: Path):
        with _frontend_only(self._old(), tmp_path) as client:
            text = client.get("/m/old/paste_solder").text

        assert 'href="/m/old/paste_solder"' not in text
        assert 'href="/machines"' in text


class TestBackendContractMismatch:
    """Backend の申告がページの前提を満たさないとき（frontend と版がずれた状態）.

    ジョブページの `job_name` / `param_specs` は backend の `GET /api/jobs` が唯一の
    出所なので、そこに無いジョブのフォームは描けない。空欄のフォームを見て実行
    ボタンを押されるより 503 を出す（`MachineClient` と同じ方針）。
    """

    def test_job_page_returns_503_when_the_backend_omits_the_job(
        self, ui_settings: UiSettings, backend_settings: ApiEnv
    ):
        with _frontend_over(
            ui_settings, backend_settings, catalog=JobCatalog()
        ) as client:
            response = client.get("/height_plane")

        assert response.status_code == 503
        assert "height_plane" in response.text
