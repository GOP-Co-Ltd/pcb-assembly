"""Frontend（`web.ui`）の SSR ページの仕様テスト.

ページを描くのは frontend で、値は backend の `GET /api/machine-info` / `/api/state` /
`/api/jobs` / `/api/settings/machine` から取る（計画書 docs/plans/web-api-ui-split.md の
「MR2」「MR4」節）。frontend は薄いラッパーなので、ここで押さえるのは次の 5 つに絞る:

- **URL 空間**: `/` の 307、未 prefix URL の 307、`/m/{machine_id}` prefix、プロキシ経路が
  pages ルータに食われないこと、未知タブ / 未知 feature の 404
- **backend が居ないとき**: 登録 0 台での案内ページ、到達不能の 503、版ずれの 503
- **値を捏造しないこと**: 未記載キーは backend の `resolved` で描き、解決できないときは
  0 で埋めず 503 にする（スライダーの value がそのまま machine.toml へ書き戻るため）
- **machine.toml が壊れても SSR が 500 しない**（設定の現在値を要る
  `_MACHINE_TOML_DEPENDENT_URLS` だけが 503 に落ちる）
- **定義から導出される配線**: ジョブの ParamSpec がフォームに出る、preview /
  ローディング UI が `JobDefinition` と一致する、`MACHINE_FIELDS` が設定フォームに出る

表示文字列・DOM id の網羅確認とページ上の実操作は `tests/e2e/` が実ブラウザで持つ。

実体の URL は `/m/{machine_id}/…` だが、既知マシンが 1 台なら未 prefix URL が 307 するので、
`follow_redirects` 既定 True の `TestClient` では prefix 無しのパスで叩ける。backend の
Settings を差し替える検証は、その backend を上流に挿した frontend を組む（`_frontend_over`）。
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import attrs
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.api.app import create_app as create_backend_app
from web.api.config_store import MACHINE_FIELDS
from web.api.jobs.catalog import JobCatalog, default_catalog
from web.api.settings import Settings as ApiSettings
from web.api.state import AppState
from web.ui.app import create_app as create_frontend_app
from web.ui.layout import (
    FEATURE_TEMPLATES,
    JOB_TEMPLATES,
    LOADING_ROTATION_PARAMS,
    TABS as PAGE_TABS,
)
from web.ui.machines import MachineEndpoint
from web.ui.settings import Settings as UiSettings

TABS = ["dev", "pasting", "pnp", "posctrl"]

# SSR で描画される全ページ（/ は 307 なので除く）
ALL_PAGE_URLS = ["/settings"] + [
    url
    for tab, features in PAGE_TABS.items()
    for url in (f"/{tab}", *(f"/{tab}/{feature}" for feature in features))
]

# machine.toml を `AppState` の防御の外で読むページ。設定フォームの現在値
# （ConfigStore / tomlkit）や paste_dispenser・pad_align の現在値をコンテキストに
# 載せるため、machine.toml がパース不能だと backend の `/api/settings/machine` が
# 500 になり、frontend は 503 ページを返す（MR2 の防御対象外）
_MACHINE_TOML_DEPENDENT_URLS = frozenset(
    {
        "/settings",
        "/pasting/paste_solder",
        "/pasting/loading",
        "/pasting/nozzle_cap",
        "/posctrl/copper_detection",
    }
)

# パース不能な machine.toml でも描画できるべきページ
ROBUST_PAGE_URLS = [
    url for url in ALL_PAGE_URLS if url not in _MACHINE_TOML_DEPENDENT_URLS
]

_OVERLAY_RADIO_RE = re.compile(r"<input[^>]*name=\"overlay\"[^>]*>")

# ジョブコンテキスト（job_name / param_specs）が注入される全ページ
JOB_PAGES = sorted(
    (tab, feature)
    for (tab, feature), template in FEATURE_TEMPLATES.items()
    if template in JOB_TEMPLATES
)


def _loading_amount_input(default: object) -> str:
    """ローディング量入力（`#lc-amount`）の value を含む HTML 断片.

    同じ既定値の別 input が同一ページに 2〜4 箇所あるため、`value="..."` 単体では
    `loading_default` の注入が消えても通ってしまう（実測）。
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
def config_dir(backend_settings: ApiSettings) -> Path:
    """上流 backend が読む config ディレクトリ（machine.toml の書き換え材料）."""
    return backend_settings.config_dir


@pytest.fixture
def appstate(backend_app: FastAPI) -> AppState:
    """上流 backend の AppState（保存済みジョブ既定値の注入に使う）."""
    return backend_app.state.appstate


@pytest.fixture
def partial_nozzle_cap(config_dir: Path) -> Path:
    """`[paste_dispenser.nozzle_cap]` に x だけを書いた machine.toml を用意する（そのパスを返す）.

    設定画面から 1 軸だけ保存すると実際にこの配置になり、`Machine.nozzle_cap` の
    `Machine` は「未記録」として扱う。`/api/state` と SSR がこれで 500 しないことをピンする
    ための素材（`AppState.nozzle_cap()` の防御が要）。
    """
    with config_dir.joinpath("machine.toml").open(
        "a", encoding="utf-8"
    ) as machine_toml:
        machine_toml.write("\n[paste_dispenser.nozzle_cap]\nx = 12.5\n")
    return config_dir / "machine.toml"


@pytest.fixture
def partial_nozzle_clean(config_dir: Path) -> Path:
    """`[paste_dispenser.nozzle_clean]` に動作値だけを書いた machine.toml を用意する.

    設定画面から押し込み量だけ保存すると座標の無いテーブルができる。`NozzleClean` は
    座標必須なので「未記録」として扱われる素材。
    """
    path = config_dir / "machine.toml"
    with path.open("a", encoding="utf-8") as machine_toml:
        machine_toml.write("\n[paste_dispenser.nozzle_clean]\npress_depth = 0.4\n")
    return path


@pytest.fixture
def broken_machine_toml(config_dir: Path) -> Path:
    """終端されていない文字列を追記して machine.toml をパース不能にする（そのパスを返す）.

    `Machine()` も `tomlkit` もこの machine.toml で例外を投げる。全ページの SSR が
    共通で使う `/api/machine-info` / `/api/state`（`machine_name()` /
    `machine_type()` / `focus_z()` の broad except → None）が効いていることをピンする
    ための素材。
    """
    with config_dir.joinpath("machine.toml").open(
        "a", encoding="utf-8"
    ) as machine_toml:
        machine_toml.write('\nbroken_key = "unterminated\n')
    return config_dir / "machine.toml"


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

    TOML としては読めるので backend の `/api/settings/machine` は 200 を返すが、
    cattrs が `PasteDispenser` を組めないため `paste_dispenser.*` の実効値
    （`resolved`）が全て None になる（backend は 1 セクションの不備で他セクションの
    実効値まで失わせないよう、キー単位で例外を潰す）。テンプレートから作った
    machine.toml に必須キーが無い実機で普通に起きる状態。
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
    backend_settings: ApiSettings,
    *,
    catalog: JobCatalog | None = None,
) -> Iterator[TestClient]:
    """Settings を差し替えた backend を上流に挿した frontend の TestClient.

    MR4 でページを描くのは frontend なので、backend の Settings を変えた影響を
    ページで見るには、その backend を上流にした frontend を組む必要がある
    （conftest の `frontend_client` は既定の `backend_settings` 固定）。

    Args:
        ui_settings: frontend 設定（登録マシン 1 台）
        backend_settings: 上流 backend の設定
        catalog: backend のジョブカタログの差し替え（版ずれの再現に使う）
    """
    backend = create_backend_app(backend_settings)
    if catalog is not None:
        backend.state.catalog = catalog
    frontend = create_frontend_app(
        ui_settings,
        transport_factory=lambda _endpoint: httpx.ASGITransport(app=backend),
    )
    try:
        with TestClient(frontend) as test_client:
            yield test_client
    finally:
        # backend の lifespan は ASGITransport では走らないので明示的に後始末する
        backend.state.preview.request_shutdown()
        backend.state.jobs.shutdown()
        backend.state.appstate.close()


class TestPages:
    """Jinja2 ページ配信."""

    def test_root_redirects_to_posctrl(
        self, client: TestClient, ui_settings: UiSettings
    ):
        """MR4: 既知マシンが 1 台なら `/m/{machine_id}/posctrl` へ 307 する.

        リダイレクト先に machine prefix が付く点だけが移設前と違う（従来は
        `/posctrl`）。prefix が無いと `/{tab}` の入口へ戻ってループする。
        """
        response = client.get("/", follow_redirects=False)

        assert response.status_code == 307
        machine_id = ui_settings.machines[0].machine_id
        assert response.headers["location"] == f"/m/{machine_id}/posctrl"

    @pytest.mark.parametrize("tab", TABS)
    def test_tab_page_renders_with_common_chrome(
        self, client: TestClient, backend_settings: ApiSettings, tab: str
    ):
        response = client.get(f"/{tab}")

        assert response.status_code == 200
        assert "緊急停止" in response.text
        assert "ファームウェア再起動" in response.text
        assert "machine-control" in response.text
        assert backend_settings.mainsail_url in response.text

    def test_file_browser_start_path_is_rendered(
        self, ui_settings: UiSettings, backend_settings: ApiSettings
    ):
        """Pcb_browse_start の pcb_browse_root 相対パスが data-fb-start に出る.

        MR4: 値は backend が `/api/machine-info` の `fb_start` で解決するので、
        差し替えた backend Settings を上流に挿した frontend でページを取る。
        """
        evolved = attrs.evolve(
            backend_settings, pcb_browse_root=backend_settings.pcb_browse_root.parent
        )
        with _frontend_over(ui_settings, evolved) as client:
            text = client.get("/posctrl").text

        start = backend_settings.pcb_browse_start.name
        assert f'data-fb-start="{start}"' in text

    def test_mainsail_link_resolves_from_machine_id_when_unset(
        self, ui_settings: UiSettings, backend_settings: ApiSettings
    ):
        """PCBASM_MAINSAIL_URL 未設定時は backend の machine_id から `.local` で解決する.

        MR2 で `request.url.hostname` フォールバックは撤去した。frontend を分離すると
        ページ閲覧元は frontend 機になるため、リクエストのホスト名を使うと必ず誤った
        Mainsail を指す。裸のホスト名だと LAN の他端末（mDNS しか引けない）から開けない
        ので `.local` を付ける。SSR も `/api/machine-info` と同じ解決結果を使う。

        MR4: frontend は backend が解決した値をそのまま描く（frontend 側に解決規則を
        持たない）。`/api/machine-info` はプロキシ経由で同じ値を返す。
        """
        settings = attrs.evolve(
            backend_settings, mainsail_url=None, hostname="paste-01"
        )
        with _frontend_over(ui_settings, settings) as client:
            text = client.get("/posctrl").text
            info = client.get("/api/machine-info").json()

        assert 'href="http://paste-01.local"' in text
        assert info["mainsail_url"] == "http://paste-01.local"
        assert "http://testserver" not in text

    def test_settings_page_renders_every_machine_field(self, client: TestClient):
        """`MACHINE_FIELDS` の全キーが即保存フォームに出る（設定キーの出し忘れの検出）.

        ペア表示（`reference_point.offsets.*`）だけは 1 入力に 2 値を載せるので
        `data-pair-key` で出る。保存ボタンは持たない（変更した時点で PUT する）。
        セクション見出しの網羅は `test_layout.py::TestSectionLabels` が持つ。
        """
        text = client.get("/settings").text

        missing = [
            spec.key
            for spec in MACHINE_FIELDS
            if f'name="{spec.key}"' not in text
            and f'data-pair-key="{spec.key}"' not in text
        ]

        assert missing == []
        assert "single-pane" in text
        assert "settings-group" in text
        assert '<label for="ms-' not in text
        assert '<button type="submit">保存</button>' not in text

    def test_unknown_tab_returns_404(self, client: TestClient):
        assert client.get("/no-such-tab").status_code == 404

    def test_unknown_feature_returns_404(self, client: TestClient):
        assert client.get("/posctrl/no-such-feature").status_code == 404


class TestStaticAssets:
    """CSS / JS のブラウザキャッシュ対策."""

    def test_page_renders_versioned_static_asset_urls(self, client: TestClient):
        text = client.get("/pasting/paste_solder").text

        assert 'href="/static/app.css?v=' in text
        assert 'src="/static/js/app.js?v=' in text
        assert 'src="/static/js/job_console.js?v=' in text
        assert 'src="/static/js/pad_editor/index.js?v=' in text

    def test_static_assets_require_browser_revalidation(self, client: TestClient):
        response = client.get("/static/app.css")

        assert response.status_code == 200
        assert response.headers["cache-control"] == (
            "no-cache, max-age=0, must-revalidate"
        )


DEV_JOB_FEATURES = (
    "extract_pcb",
    "make_fill_coverage_pcb",
)


class TestDevJobPages:
    """Phase 3: dev タブのジョブページと klipper_status ページ."""

    @pytest.mark.parametrize("feature", DEV_JOB_FEATURES)
    def test_dev_job_page_renders_job_console(self, client: TestClient, feature: str):
        response = client.get(f"/dev/{feature}")

        assert response.status_code == 200
        assert "job-console" in response.text
        # data-job-name 等でページのジョブ名が宣言される
        assert feature in response.text

    def test_job_demo_is_hidden_from_sidebar(self, client: TestClient):
        assert "job_demo" not in client.get("/dev").text


POSCTRL_JOB_FEATURES = ("camera_calibration", "board_tour", "orthogonality_test")


class TestPosctrlJobPages:
    """Phase 4: posctrl のジョブページ（計画書 webui-phase4.md「templates /
    static」節 + spec §10 posctrl 表）."""

    @pytest.mark.parametrize("feature", POSCTRL_JOB_FEATURES)
    def test_posctrl_job_page_renders_console_preview_and_overlay_switch(
        self, client: TestClient, feature: str
    ):
        response = client.get(f"/posctrl/{feature}")

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
    def test_posctrl_job_page_default_overlay(
        self, client: TestClient, feature: str, expected: str
    ):
        """ツアー系（board_tour / orthogonality_test）は十字線を既定 ON で描画する.

        ツアーは十字線を基準に位置を目視合わせするため、ユーザーがラジオを操作せずとも
        十字線が出ている必要がある。camera_calibration / camera_preview は素の映像を見る
        ページなので none。
        """
        assert _checked_overlay(client.get(f"/posctrl/{feature}").text) == expected

    def test_camera_calibration_renders_square_size_form_with_default(
        self, client: TestClient
    ):
        """ジョブパラメータフォームは square_size のみ（初回既定値 1.5mm）。専用 JS を読み込む."""
        text = client.get("/posctrl/camera_calibration").text

        assert 'name="square_size"' in text
        assert 'value="1.5"' in text
        assert "js/camera_calibration.js" in text
        # crop は job param から削除済み（machine.toml 連動の即保存フォームへ移設）
        assert "crop_width" not in text
        assert "crop_height" not in text

    def test_reference_point_setup_page_renders_record_and_quit(
        self, client: TestClient
    ):
        response = client.get("/posctrl/reference_point_setup")

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
    """Pasting のジョブページ（計画書 webui-phase5.md「templates / static」節）.

    preview / ローディング UI の有無と段階は `TestJobDefinitionDrivenContext` が
    ジョブ定義から導出して検証するので、ここに残すのはページ固有の配線だけ。
    """

    PASTE_SOLDER_SETTING_KEYS = (
        "paste_dispenser.auto_line_aspect_ratio",
        "paste_dispenser.auto_area_short_side_factor",
        "paste_dispenser.flow_calibration.calibration_file",
        "paste_dispenser.flow_calibration.amount_ul",
        "paste_dispenser.flow_calibration.crop_size_mm",
        "paste_dispenser.flow_calibration.settle_seconds",
    )

    @pytest.mark.parametrize("setting_key", PASTE_SOLDER_SETTING_KEYS)
    def test_paste_solder_renders_the_machine_settings_form(
        self, client: TestClient, setting_key: str
    ):
        """Auto しきい値と流量キャリブ設定が machine 設定の即保存フォームに出る.

        操作の実体（保存・校正ファイルの選択・測定位置の指定）は e2e が実ブラウザで持つので、
        ここは「設定キーが即保存フォームに載っている」ことだけを見る。
        """
        text = client.get("/pasting/paste_solder").text

        assert "data-machine-settings" in text
        assert 'data-endpoint="/api/settings/machine"' in text
        assert setting_key in text

    def test_loading_page_renders_optional_start_position(self, client: TestClient):
        text = client.get("/pasting/loading").text

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
        text = client.get("/pasting/loading").text

        assert 'id="loading-mass-calibration"' in text
        assert 'data-job-names="loading"' in text

    def test_dispense_calibration_renders_the_calibration_menu(
        self, client: TestClient
    ):
        """①②③/全実行/終了メニューと、フォームのセクション分けが出る."""
        text = client.get("/pasting/dispense_calibration").text

        assert "job-param-group" in text
        for legend in (
            "銅板・位置合わせ（キャリブ後固定）",
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


class TestJobParamForms:
    """ジョブの ParamSpec がフォームへ届く（`ParamSpec` を足したのに UI に出ない事故の検出）."""

    @pytest.mark.parametrize(("tab", "feature"), JOB_PAGES)
    def test_every_job_param_has_an_input(
        self, client: TestClient, backend_app: FastAPI, tab: str, feature: str
    ):
        definition = backend_app.state.catalog.get(feature)
        assert definition is not None, feature

        text = client.get(f"/{tab}/{feature}").text

        missing = [
            spec.name
            for spec in definition.params
            if f'id="param-{spec.name}"' not in text
        ]
        assert missing == [], feature


class TestSavedJobParamDefaults:
    """入力途中の即保存値（param-defaults）が次回描画の default に反映される.

    ページごとに別テンプレートを使うが、`appstate.merge_job_param_defaults` した値が
    `value="..."` に出るという同じ機構で動く。
    """

    @pytest.mark.parametrize(
        ("url", "job_name", "defaults", "fragments"),
        (
            pytest.param(
                "/posctrl/camera_calibration",
                "camera_calibration",
                {"square_size": 3.0},
                ('value="3.0"',),
                id="camera_calibration",
            ),
            pytest.param(
                "/pasting/loading",
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
                "/pasting/dispense_calibration",
                "dispense_calibration",
                {"board_width": 30.0, "line_count": 8, "speed_max": 12.0},
                ('value="30.0"', 'value="8"', 'value="12.0"'),
                id="dispense_calibration",
            ),
            pytest.param(
                "/pasting/toolhead_offset",
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
        appstate: AppState,
        url: str,
        job_name: str,
        defaults: dict[str, float],
        fragments: tuple[str, ...],
    ):
        appstate.merge_job_param_defaults(job_name, defaults)

        text = client.get(url).text

        for fragment in fragments:
            assert fragment in text, fragment


class TestJobDefinitionDrivenContext:
    """Preview / ローディング UI は JobDefinition から導出する（MR2）.

    以前は pages.py の 3 つのハードコード辞書と `if tab == "pasting"` 特別扱いが正
    だった。preview フレームの提供有無と progress_stage 文字列は backend の事実なので
    ジョブ定義側へ移し、ページはそれを読むだけにした（`/api/jobs` と同じ値になる）。
    """

    @pytest.mark.parametrize("feature", PASTING_JOB_FEATURES)
    def test_preview_pane_presence_matches_provides_preview(
        self, client: TestClient, backend_app: FastAPI, feature: str
    ):
        definition = backend_app.state.catalog.get(feature)

        text = client.get(f"/pasting/{feature}").text

        assert ("preview-pane" in text) is definition.provides_preview
        # pasting の preview は固定 none（overlay 切替は posctrl のページだけ）
        assert "crosshair" not in text

    @pytest.mark.parametrize("feature", PASTING_JOB_FEATURES)
    def test_loading_controls_derive_stage_and_default_from_definition(
        self, client: TestClient, backend_app: FastAPI, feature: str
    ):
        """Loading UI の段階と既定値が定義側の値と一致する.

        `loading.html` / `dispense_calibration.html` は loading_controls を
        `show_loading_controls` で gate せず無条件 include するため、この 2 feature では
        `"loading-controls" in text` が定数 True になる（検出力ゼロ）。段階文字列と
        `#lc-amount` の value を見て、全 feature で定義との一致を確かめる。
        """
        definition = backend_app.state.catalog.get(feature)

        text = client.get(f"/pasting/{feature}").text

        if definition.loading_param is None:
            assert "loading-controls" not in text
            return
        assert f'data-loading-stage="{definition.loading_stages}"' in text
        default = next(
            spec.default
            for spec in definition.params
            if spec.name == definition.loading_param
        )
        assert _loading_amount_input(default) in text


class TestProbeGuidePage:
    """ロードセルプローブのガイドページ（計画書「WebUI ガイドページ」節）.

    ジョブではない静的な説明ページ。旧サーボ方式の probe_gnd_down_adjust ジョブは削除済みで、タブ・URL
    のどちらからも到達できない。
    """

    def test_probe_guide_page_renders_calibration_steps(self, client: TestClient):
        response = client.get("/pasting/probe_guide")

        assert response.status_code == 200
        # 較正手順（Klipper コンソールで実行するコマンド）と公式ドキュメントリンク
        assert "LOAD_CELL_CALIBRATE" in response.text
        assert "SAVE_CONFIG" in response.text
        assert "klipper3d.org" in response.text


class TestNozzleCapPage:
    """ノズルキャップ位置の設定ページ（nozzle-cap-parking 計画書「WebUI」節）.

    ジョブではない静的な設定ページ（非ジョブテンプレート）。現在値が未記録 （repo fixture に
    [paste_dispenser.nozzle_cap] なし）のときは「未記録」を表示する。
    """

    def test_partially_recorded_cap_shows_placeholder(
        self, partial_nozzle_cap: Path, client: TestClient
    ):
        """設定画面から X だけ保存した状態でも 500 にせず「未記録」を出す（MR2）.

        `Machine.nozzle_cap` の structure は x/y/z 必須なので、`state.machine()` を
        直接読んでいた頃はこのページだけが 500 していた。nozzle_cap を読むのは
        このページと `/api/state`・`move_to_cap` だけで、他ページの SSR には載らない。
        """
        response = client.get("/pasting/nozzle_cap")

        assert response.status_code == 200
        assert "未記録" in response.text

    def test_shows_cleaning_section_with_record_button(self, client: TestClient):
        """キャップと同じページでクリーニング位置も記録できる."""
        response = client.get("/pasting/nozzle_cap")

        assert response.status_code == 200
        assert 'data-testid="nozzle-clean-current"' in response.text
        assert 'data-testid="nozzle-clean-record"' in response.text

    def test_shows_cleaning_settings_form(self, client: TestClient):
        """押し込み量・こすり幅は結果を見て追い込む値なので記録ボタンと同じ画面に置く."""
        response = client.get("/pasting/nozzle_cap")

        assert 'data-testid="nozzle-clean-settings"' in response.text
        assert 'name="paste_dispenser.nozzle_clean.press_depth"' in response.text
        # 座標は記録ボタンの管轄なので手打ち欄を並べない
        assert 'name="paste_dispenser.nozzle_clean.x"' not in response.text

    def test_partially_recorded_clean_shows_placeholder(
        self, partial_nozzle_clean: Path, client: TestClient
    ):
        """座標の無い `[paste_dispenser.nozzle_clean]` でも 500 にせず「未記録」を出す."""
        response = client.get("/pasting/nozzle_cap")

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

        response = client.get("/pasting/nozzle_cap")

        # 同じページの設定ラベルに当たらないよう、label 文字列そのものを見る
        assert "(10.00, 20.00, -30.00) mm" in response.text


class TestBrokenMachineTomlPages:
    """パース不能な machine.toml でも SSR が落ちない（MR2）.

    `_base_context` は全ページで `machine_name()` / `machine_type()` / `focus_z()` を
    呼ぶ。この 3 つの防御（broad except → None）を外すと、machine.toml が壊れただけで
    ページが軒並み 500 する。設定フォームの現在値や paste_dispenser の現在値を
    コンテキストに載せるページ（`_MACHINE_TOML_DEPENDENT_URLS`）は machine.toml を
    防御の外で読むため対象外＝この防御では守れない（MR2 の範囲外の別課題）。
    """

    @pytest.mark.parametrize("url", ROBUST_PAGE_URLS)
    def test_broken_machine_toml_keeps_pages_renderable(
        self, broken_machine_toml: Path, client: TestClient, url: str
    ):
        assert client.get(url).status_code == 200

    @pytest.mark.parametrize("url", sorted(_MACHINE_TOML_DEPENDENT_URLS))
    def test_machine_toml_dependent_pages_return_503(
        self, broken_machine_toml: Path, client: TestClient, url: str
    ):
        """設定の現在値を要るページは 500 ではなく 503 を返す（MR4）.

        frontend が全ページで `/api/settings/machine` を取ると、machine.toml が
        壊れただけで案内も出せない 503 が全画面に出る。取得を必要なページに限って
        いることの裏返しとして、必要なページだけがここに落ちる。
        """
        assert client.get(url).status_code == 503


class TestUnsetMachineSettingsShowResolvedValues:
    """machine.toml に未記載のキーは backend が解決した実効値で描く（MR4）.

    frontend が「未記載 → 0.0」で埋めると、copper_detection は canny 0 / blur_ksize 0 を
    描き、その 0 が「設定に保存」で machine.toml へ書き戻されて銅箔検出が壊れる
    （loading は密度 0.000 mg/uL を出す）。既定値の出所は backend の `resolved` だけに
    保ち、frontend は既定値を持たない。
    """

    def test_copper_detection_renders_resolved_canny_values(
        self, machine_toml_without_defaulted_keys: Path, client: TestClient
    ):
        text = client.get("/posctrl/copper_detection").text

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
        """位置だけ記録した状態でも、動作設定は既定値で描く.

        記録直後がこの状態なので、ここで空欄になると既定値で動いているのに「未設定」に
        見える。既定値の出所は backend の `resolved` だけに保つ。
        """
        path = config_dir / "machine.toml"
        with path.open("a", encoding="utf-8") as machine_toml:
            machine_toml.write(
                "\n[paste_dispenser.nozzle_clean]\nx = 10.0\ny = 20.0\nz = -30.0\n"
            )

        text = client.get("/pasting/nozzle_cap").text

        # NozzleClean の既定値（press_depth=0.5 / stroke=2.0 / passes=2）
        assert 'name="paste_dispenser.nozzle_clean.press_depth"' in text
        assert 'value="0.5"' in text
        assert 'value="2.0"' in text
        assert 'value="2"' in text

    def test_loading_renders_resolved_paste_density(
        self, machine_toml_without_defaulted_keys: Path, client: TestClient
    ):
        text = client.get("/pasting/loading").text

        # PasteDispenser.solder_paste_density の既定値
        assert '<dd>3.780 <span class="unit">mg/uL</span></dd>' in text
        assert '<dd>0.000 <span class="unit">mg/uL</span></dd>' not in text
        # machine.toml に書かれているキーは書かれている値のまま
        assert ">45.783133</output>" in text

    def test_paste_solder_renders_resolved_refinement_threshold(
        self, machine_toml_without_defaulted_keys: Path, client: TestClient
    ):
        response = client.get("/pasting/paste_solder")

        assert response.status_code == 200
        assert "逐次位置合わせ対象の最大短辺" in response.text
        input_tag = re.search(
            r'<input[^>]*name="paste_dispenser\.pad_align\.'
            r'refine_max_short_side"[^>]*>',
            response.text,
        )
        assert input_tag is not None
        assert 'data-type="float"' in input_tag.group()
        assert 'value="0.4"' in input_tag.group()
        assert '<span class="unit">mm</span>' in response.text


class TestUnresolvableMachineSettingsAreNotFabricated:
    """実効値が解決できないときは 0 を捏造せず 503 を返す（MR4 / M1）.

    backend が落ちているわけではない（`/api/settings/machine` は 200）ので、frontend が
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
        「実効値が無いから」であることを、上流の応答側から示す。
        """
        machine_id = ui_settings.machines[0].machine_id

        response = client.get(f"/m/{machine_id}/api/settings/machine")

        assert response.status_code == 200
        fields = {field["key"]: field for field in response.json()["fields"]}
        assert fields["paste_dispenser.pad_align.canny_low"]["resolved"] is None
        assert fields["paste_dispenser.rotations_per_ul"]["value"] == 45.783133
        # 欠けているのは [paste_dispenser] だけ = 他セクションの実効値は残る
        assert fields["probe.min_samples"]["resolved"] == 6

    def test_copper_detection_returns_503_instead_of_zero(
        self,
        machine_toml_without_required_paste_dispenser_key: Path,
        client: TestClient,
    ):
        response = client.get("/posctrl/copper_detection")

        assert response.status_code == 503
        # 0 を描いたページを見せない（スライダーの value がそのまま PUT される）
        assert "canny-low-value" not in response.text


class TestMachineControlCapButton:
    """マシン操作パネルの「キャップ位置に移動」ボタン（paste マシン限定表示）.

    nozzle-cap-parking 計画書「WebUI」節: machine_control.html は machine_type
    == "paste" のときのみ #mc-move-to-cap を出す。machine_type が読めない config でも
    ページは 500 にならずボタンを隠す（state.machine_type() は broad except → None）。
    """

    @pytest.mark.parametrize(
        ("drop_machine_type", "expected"),
        (
            pytest.param(False, True, id="paste マシン"),
            pytest.param(True, False, id="machine_type が読めない"),
        ),
    )
    def test_move_to_cap_button_follows_the_machine_type(
        self,
        client: TestClient,
        config_dir: Path,
        drop_machine_type: bool,
        expected: bool,
    ):
        if drop_machine_type:
            path = config_dir / "machine.toml"
            path.write_text(
                "".join(
                    line
                    for line in path.read_text(encoding="utf-8").splitlines(
                        keepends=True
                    )
                    if not line.startswith("machine_type")
                ),
                encoding="utf-8",
            )

        response = client.get("/posctrl")

        assert response.status_code == 200
        assert ("mc-move-to-cap" in response.text) is expected


class TestPasteVolumeCalibrationLoading:
    """塗布量校正の生成ページのローディング操作 UI.

    pad editor の DOM フックと表示要素は e2e が実ブラウザで操作する。
    """

    def test_volume_and_rotation_loading_derive_from_the_job_definition(
        self, client: TestClient, backend_app: FastAPI
    ):
        """塗布パス先頭のローディングで体積・回転の両方を操作できる.

        回転セクションは `loading_rotation_defaults`（ジョブの `loading_*` ParamSpec から
        組む）が渡ったときだけ描かれるので、ParamSpec の名前を変えるとフォーム欄だけが
        増えて操作が効かない状態になる。
        """
        definition = backend_app.state.catalog.get("paste_volume_calibration")
        param_names = {spec.name for spec in definition.params}

        text = client.get("/pasting/paste_volume_calibration").text

        assert 'id="lc-amount"' in text
        for name in LOADING_ROTATION_PARAMS:
            assert f"loading_{name}" in param_names, name
            assert f'id="lc-{name.replace("_", "-")}"' in text, name


class TestTabsCatalogConsistency:
    """TABS（サイドバー掲載）と catalog（ジョブ登録）の整合."""

    def test_every_visible_job_is_listed_in_its_tab(self):
        """Hidden 以外の全ジョブは所属タブの TABS に掲載されている."""
        for definition in default_catalog().list():
            if definition.hidden:
                continue
            assert (
                definition.name in PAGE_TABS[definition.tab]
            ), f"{definition.name} が TABS[{definition.tab!r}] に掲載されていません"


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
    """`/m/{machine_id}` prefix の URL 空間（MR4）.

    未 prefix の URL は入口（307）で、実体は prefix 付き。prefix が落ちると ブラウザは同居 backend
    や別マシンを叩いてしまうため、リンク・フォーム・ ドロップダウンの遷移先すべてに prefix が乗っている必要がある。
    """

    def test_unprefixed_feature_url_redirects_to_the_only_machine(
        self, client: TestClient, ui_settings: UiSettings
    ):
        machine_id = ui_settings.machines[0].machine_id

        response = client.get("/pasting/loading", follow_redirects=False)

        assert response.status_code == 307
        assert response.headers["location"] == f"/m/{machine_id}/pasting/loading"

    def test_every_navigation_link_is_machine_prefixed(
        self, client: TestClient, ui_settings: UiSettings
    ):
        """タブ・サイドバー・設定リンクと JS の base が prefix 付きになる.

        `app.js` は `data-machine-base` を全 `fetch` の prefix に使うので、この属性が
        落ちると frontend 自身に `/api/...` を投げて 404 になる。
        """
        machine_id = ui_settings.machines[0].machine_id
        base = f"/m/{machine_id}"

        text = client.get(f"{base}/pasting").text

        assert f'data-machine-base="{base}"' in text
        assert f'href="{base}/posctrl"' in text
        assert f'href="{base}/settings"' in text
        assert f'href="{base}/pasting/loading"' in text
        # 未 prefix の内部リンクが 1 本も残っていない（外部リンクは mainsail のみ）。
        # /static は意図的に prefix しない（キャッシュを全マシンで 1 本共有する）
        internal = [
            href
            for href in re.findall(r'href="(/[^"]*)"', text)
            if not href.startswith("/static/")
        ]
        assert internal
        assert [href for href in internal if not href.startswith(f"{base}/")] == []

    def test_api_path_is_proxied_and_not_eaten_by_the_pages_router(
        self, client: TestClient, ui_settings: UiSettings
    ):
        """`/m/{id}/api/state` はプロキシに届く（登録順の回帰）.

        pages ルータを先に登録すると `/m/{machine_id}/{tab}/{feature}` が
        `/m/x/api/state` を飲み込み、404 HTML が返って UI 全体が動かなくなる。
        """
        machine_id = ui_settings.machines[0].machine_id

        response = client.get(f"/m/{machine_id}/api/state")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert "busy" in response.json()

    def test_artifacts_path_is_proxied_and_not_eaten_by_the_pages_router(
        self, client: TestClient, ui_settings: UiSettings, backend_settings: ApiSettings
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

        text = client.get(f"/m/{machine.machine_id}/posctrl/camera_preview").text

        assert f'value="/m/{machine.machine_id}/posctrl/camera_preview"' in text
        assert machine.label in text

    def test_machine_id_alone_redirects_to_the_default_tab(
        self, client: TestClient, ui_settings: UiSettings
    ):
        """`/m/{id}` 単体は既定タブへ 307 する.

        この登録が `/{tab}/{feature}` より後だと tab="m" / feature=machine_id として
        食われ、`/m/{id}/m/{id}` へ 307 したうえで 404 になる。
        """
        machine_id = ui_settings.machines[0].machine_id

        response = client.get(f"/m/{machine_id}", follow_redirects=False)

        assert response.status_code == 307
        assert response.headers["location"] == f"/m/{machine_id}/posctrl"

    def test_unknown_machine_returns_404_page_with_the_selector(
        self, client: TestClient, ui_settings: UiSettings
    ):
        """未知の machine_id は 404 HTML（ドロップダウンから戻れる）."""
        response = client.get("/m/no-such-machine/posctrl")

        assert response.status_code == 404
        assert response.headers["content-type"].startswith("text/html")
        assert ui_settings.machines[0].label in response.text
        # 未知のマシンを base にするとタブが全部 404 になるので prefix は付けない
        assert 'href="/posctrl"' in response.text


class TestWithoutMachines:
    """マシン登録 0 台（machine.toml が無いホストでの単独起動）."""

    def test_create_app_succeeds_and_serves_guidance(self, tmp_path: Path):
        """機体設定を一切読まずに起動でき、案内ページを返す.

        frontend が `get_config_dir()` や machine.toml に触ると、機体でない PC で
        起動できなくなる（MR4 の要件）。`/settings` も backend 無しで開ける。
        """
        with _frontend_only((), tmp_path) as client:
            response = client.get("/")
            settings_response = client.get("/settings")

        assert response.status_code == 200
        assert "machines.toml" in response.text
        # 選ぶ先が無いのでドロップダウンは出さない
        assert 'data-testid="machine-select"' not in response.text
        assert settings_response.status_code == 200


class TestDefaultBackendPort:
    """Port を省略した `machines.toml` の登録に使う port（MR4）.

    `PCBASM_UI_DEFAULT_BACKEND_PORT` を設定できるのに効かない（`load_machines_file` が
    module 定数を直参照する）状態は、host しか判らないマシンを扱う MR5 で
    「設定したのに 8081 へ繋ぐ」形で表面化する。
    """

    def test_env_default_port_applies_to_entries_without_a_port(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        machines_file = tmp_path / "machines.toml"
        machines_file.write_text(
            '[[machine]]\nmachine_id = "alpha"\nhost = "127.0.0.1"\n', encoding="utf-8"
        )
        monkeypatch.setenv("PCBASM_UI_MACHINES_FILE", str(machines_file))
        monkeypatch.setenv("PCBASM_UI_DEFAULT_BACKEND_PORT", "19999")

        # settings 未指定 = Settings.from_env()（uvicorn --factory と同じ経路）
        with TestClient(create_frontend_app()) as client:
            response = client.get("/m/alpha/posctrl")

        # 誰も listen していない port なので 503。到達を試みた URL が本文に出る
        assert response.status_code == 503
        assert "http://127.0.0.1:19999" in response.text


class TestMultipleMachines:
    """マシンが複数登録されているとき（どれを開くかは選ばせる）."""

    @staticmethod
    def _machines() -> tuple[MachineEndpoint, ...]:
        return (
            MachineEndpoint(machine_id="alpha", host="alpha.local", port=8081),
            MachineEndpoint(
                machine_id="bravo", host="bravo.local", port=8081, name="2 号機"
            ),
        )

    def test_root_shows_a_picker_instead_of_redirecting(self, tmp_path: Path):
        with _frontend_only(self._machines(), tmp_path) as client:
            response = client.get("/", follow_redirects=False)

        assert response.status_code == 200
        for machine in self._machines():
            assert machine.label in response.text

    def test_picker_options_keep_the_requested_page(self, tmp_path: Path):
        """未 prefix のページで選ばせるときも遷移先はそのページ."""
        with _frontend_only(self._machines(), tmp_path) as client:
            text = client.get("/pasting/loading").text

        assert 'value="/m/alpha/pasting/loading"' in text
        assert 'value="/m/bravo/pasting/loading"' in text


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
            response = client.get("/m/dead/posctrl")

        assert response.status_code == 503
        assert response.headers["content-type"].startswith("text/html")
        # ドロップダウンは frontend の登録一覧から描くので backend 不要 = 必ず出る
        assert 'data-testid="machine-select"' in response.text
        assert self._dead()[0].label in response.text

    def test_page_asks_the_browser_to_retry(self, tmp_path: Path):
        with _frontend_only(self._dead(), tmp_path) as client:
            response = client.get("/m/dead/posctrl")

        assert response.headers["retry-after"] == "5"


class TestBackendContractMismatch:
    """Backend の申告がページの前提を満たさないとき（frontend と版がずれた状態）.

    ジョブページの `job_name` / `param_specs` は backend の `GET /api/jobs` が唯一の
    出所なので、そこに無いジョブのフォームは描けない。空欄のフォームを見て実行
    ボタンを押されるより 503 を出す（`MachineClient` と同じ方針）。
    """

    def test_job_page_returns_503_when_the_backend_omits_the_job(
        self, ui_settings: UiSettings, backend_settings: ApiSettings
    ):
        with _frontend_over(
            ui_settings, backend_settings, catalog=JobCatalog()
        ) as client:
            response = client.get("/pasting/height_plane")

        assert response.status_code == 503
        assert "height_plane" in response.text
