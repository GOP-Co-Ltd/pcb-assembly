"""`web.api.routers.pages` の仕様テスト.

計画書「routers」節 + spec §10:

- `GET /` → `/posctrl` へ 307 リダイレクト
- 4 タブ + 既知 feature + `/settings` が 200
- 全ページ共通の chrome（安全操作・マシン操作パネル・mainsail console リンク）
- 未知タブ / 未知 feature → 404

Phase 2 追記（計画書 webui-phase2.md「既存ルーターへの変更」節 + spec §10）:

- posctrl の camera_preview / copper_detection は専用テンプレート、
  他 feature は従来プレースホルダのまま

Phase 3 追記（計画書 webui-phase3.md「templates / static」節 + spec §10 dev タブ）:

- dev のジョブ 4 ページは ParamSpec 由来のフォーム + job-console
- klipper_status ページは G-code 送信ボックス + limits 表示
- job_demo（hidden）はサイドバーに出ない

Phase 4 追記（計画書 webui-phase4.md「templates / static」節 + spec §10 posctrl）:

- posctrl のジョブ 3 ページは preview ペイン（overlay 切替）+ フォーム + job-console
- reference_point_setup は preview + Record / Quit ボタン（ジョグはマシン操作
  パネルのジョブモード）
- 未実装プレースホルダの確認対象は pasting（Phase 5）へ移行

Phase 5 追記（計画書 webui-phase5.md「routers/pages.py」「templates / static」節
+ spec §10 pasting 表）:

- pasting 7 feature ページは全て job-console + job-form
- preview ペイン（overlay 切替なし）は paste_solder / height_plane /
  toolhead_offset のみ
- loading_controls は paste_solder / loading / toolhead_offset（stage="ローディング"）と
  dispense_calibration（stage="キャリブレーションメニュー,ローディング"・プライム/① ローディング用）
- pnp はプレースホルダのみ（サイドバー空 + 「機能を選択」）

webui-camera-calib 計画書「公開インターフェース案 5」+ 要確認事項 1・2 が
camera_calibration ページへ追記契約:

- ジョブパラメータフォームは square_size のみ（初回既定値 1.5）。crop_width /
  crop_height は job param から削除済み
- 専用 JS（camera_calibration.js）を読み込む（square_size の入力時復元保存用）

ユーザー追加指示（crop 編集 UI の統一）が上書き契約:

- crop の編集 UI は settings ページの汎用即保存フォームのみに統一する。
  camera_calibration ページに crop 入力（data-machine-key /
  `#camera-crop-settings`）は置かない
- settings ページには camera.crop.width / camera.crop.height が
  machine 設定として描画される（「カメラ / クロップ」セクション）

直行性テストのライブ十字線対応（fix/20260727/orthogonality-interactive）が追記契約:

- overlay ラジオの初期選択はページ変数 `preview_overlay`（既定 "none"）で決まる。
  posctrl のツアー系ジョブページ（board_tour / orthogonality_test）は "crosshair"、
  他ページは "none" のまま

MR2（計画書 docs/plans/web-api-ui-split.md「MR2」節）が上書き契約:

- mainsail リンクは backend が解決した値を使う。`request.url.hostname` フォールバックは
  撤去した（プロキシ配下では frontend 機を指してしまい必ず誤る）。未設定時は
  `http://{machine_id}.local`（LAN の他端末からは mDNS の `*.local` しか引けない）
- preview ペイン / ローディング UI の有無は `JobDefinition` の provides_preview /
  loading_param / loading_stages から導出する。pages.py の 3 つのハードコード辞書と
  `if tab == "pasting"` 特別扱いは撤去し、`JobDefinition.provides_preview` /
  `JobDefinition.loading_param` / `JobDefinition.loading_stages` に移した
- `machine_name` は `SECTION_LABELS` の「マシン」セクションに出る（トップレベルの
  bare key なので、エントリが無いと `<summary>` に生キーが出る）
- machine.toml がパース不能でも、`AppState` の防御を通るページは 200 を返す
"""

import re
from pathlib import Path

import attrs
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.api.app import create_app
from web.api.jobs.catalog import default_catalog
from web.api.routers.pages import TABS as PAGE_TABS
from web.api.settings import Settings
from web.api.state import AppState

TABS = ["dev", "pasting", "pnp", "posctrl"]

# SSR で描画される全ページ（/ は 307 なので除く）
ALL_PAGE_URLS = ["/settings"] + [
    url
    for tab, features in PAGE_TABS.items()
    for url in (f"/{tab}", *(f"/{tab}/{feature}" for feature in features))
]

# machine.toml を `AppState` の防御の外で読むページ。設定フォームの現在値
# （ConfigStore / tomlkit）や paste_dispenser・pad_align の現在値をコンテキストに
# 載せるため、machine.toml がパース不能だと 500 になる（MR2 の防御対象外）
_MACHINE_TOML_DEPENDENT_URLS = frozenset(
    {
        "/settings",
        "/pasting/paste_solder",
        "/pasting/loading",
        "/posctrl/copper_detection",
    }
)

# パース不能な machine.toml でも描画できるべきページ
ROBUST_PAGE_URLS = [
    url for url in ALL_PAGE_URLS if url not in _MACHINE_TOML_DEPENDENT_URLS
]

_OVERLAY_RADIO_RE = re.compile(r"<input[^>]*name=\"overlay\"[^>]*>")


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


class TestPages:
    """Jinja2 ページ配信."""

    def test_root_redirects_to_posctrl(self, client: TestClient):
        response = client.get("/", follow_redirects=False)

        assert response.status_code == 307
        assert response.headers["location"] == "/posctrl"

    @pytest.mark.parametrize("tab", TABS)
    def test_tab_page_renders_with_common_chrome(
        self, client: TestClient, webui_settings: Settings, tab: str
    ):
        response = client.get(f"/{tab}")

        assert response.status_code == 200
        assert "緊急停止" in response.text
        assert "ファームウェア再起動" in response.text
        assert "machine-control" in response.text
        assert webui_settings.mainsail_url in response.text

    def test_tabs_render_japanese_labels(self, client: TestClient):
        text = client.get("/posctrl").text

        for label in ("開発", "はんだ塗布", "部品実装", "位置合わせ"):
            assert label in text

    def test_file_browser_start_path_is_rendered(self, webui_settings: Settings):
        """Pcb_browse_start の pcb_browse_root 相対パスが data-fb-start に出る."""
        evolved = attrs.evolve(
            webui_settings, pcb_browse_root=webui_settings.pcb_browse_root.parent
        )
        app = create_app(evolved)
        with TestClient(app) as client:
            text = client.get("/posctrl").text

        start = webui_settings.pcb_browse_start.name
        assert f'data-fb-start="{start}"' in text

    def test_mainsail_link_resolves_from_machine_id_when_unset(
        self, webui_settings: Settings
    ):
        """PCBASM_MAINSAIL_URL 未設定時は backend の machine_id から `.local` で解決する.

        MR2 で `request.url.hostname` フォールバックは撤去した。frontend を分離すると
        ページ閲覧元は frontend 機になるため、リクエストのホスト名を使うと必ず誤った
        Mainsail を指す。裸のホスト名だと LAN の他端末（mDNS しか引けない）から開けない
        ので `.local` を付ける。SSR も `/api/machine-info` と同じ解決結果を使う。
        """
        settings = attrs.evolve(webui_settings, mainsail_url=None, hostname="paste-01")
        with TestClient(create_app(settings)) as client:
            text = client.get("/posctrl").text
            info = client.get("/api/machine-info").json()

        assert 'href="http://paste-01.local"' in text
        assert info["mainsail_url"] == "http://paste-01.local"
        assert "http://testserver" not in text

    def test_settings_page_labels_the_machine_name_section(self, client: TestClient):
        """トップレベル bare key の machine_name は「マシン」セクションに出る（MR2）.

        `section_of` はドットを含まない key をそのまま返すため、`SECTION_LABELS` に
        エントリが無いと `<summary>` に生キー `machine_name` が出る。
        """
        text = client.get("/settings").text

        assert "<summary>マシン</summary>" in text
        assert '<span class="settings-label">マシン名</span>' in text
        assert "<summary>machine_name</summary>" not in text

    def test_known_feature_page_renders(self, client: TestClient):
        response = client.get("/posctrl/reference_point_setup")

        assert response.status_code == 200

    def test_settings_page_renders(self, client: TestClient):
        response = client.get("/settings")

        assert response.status_code == 200
        assert "settings-layout" in response.text
        assert 'name="probe.lift_height"' in response.text
        assert "プローブ後の上昇高さ" in response.text
        assert 'name="probe.board_edge_margin"' in response.text
        assert "基板外形からの最小距離" in response.text
        assert 'data-pair-key="reference_point.offsets.top_left"' in response.text
        assert 'class="settings-label"' in response.text
        assert '<label for="ms-' not in response.text
        assert '<button type="submit">保存</button>' not in response.text

    def test_settings_page_groups_fields_by_section(self, client: TestClient):
        """設定項目はセクション単位の階層表示（settings-group）でまとまる."""
        text = client.get("/settings").text

        assert "settings-group" in text
        for section_label in (
            "ペーストディスペンサー",
            "ペーストディスペンサー / パッド位置合わせ",
            "プローブ",
            "基準点",
            "基準点 / コーナーオフセット",
            "カメラ",
            "カメラ / クロップ",
        ):
            assert section_label in text
        # モーション設定（printer.cfg）は Mainsail 直編集に移行し画面から削除済み
        assert "モーション設定" not in text

    def test_settings_page_renders_camera_crop_fields(self, client: TestClient):
        """クロップ編集 UI は settings ページの汎用フォームに統一されている.

        camera_calibration ページからは crop 入力を撤去し、machine 設定の汎用フォーム
        （camera.crop.width / camera.crop.height）へ一本化した。 ユーザー追加指示: 'crop
        値の編集 UI を settings ページのみに置く形に統一'。
        """
        text = client.get("/settings").text

        assert 'name="camera.crop.width"' in text
        assert 'name="camera.crop.height"' in text
        assert "クロップ幅" in text
        assert "クロップ高さ" in text

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


class TestCompletionNotice:
    """ジョブ終了通知の全ページ共通 DOM 契約。"""

    def test_pasting_page_renders_persistent_notice_hooks(self, client: TestClient):
        text = client.get("/pasting/paste_solder").text

        assert 'id="job-completion-notice"' in text
        assert 'id="job-completion-message"' in text
        assert 'id="job-completion-dismiss"' in text
        assert (
            'data-success-sound-url="/static/audio/paste-completion-success.wav?v='
            in text
        )
        assert (
            'data-failure-sound-url="/static/audio/paste-completion-failure.wav?v='
            in text
        )
        assert 'aria-live="assertive"' in text
        assert "hidden" in text

    @pytest.mark.parametrize(
        "filename",
        ("paste-completion-success.wav", "paste-completion-failure.wav"),
    )
    def test_completion_sound_asset_is_served(self, client: TestClient, filename: str):
        response = client.get(f"/static/audio/{filename}")

        assert response.status_code == 200
        assert response.content.startswith(b"RIFF")


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

    def test_klipper_status_page_renders_gcode_box_and_limits(self, client: TestClient):
        response = client.get("/dev/klipper_status")

        assert response.status_code == 200
        assert "gcode" in response.text
        assert "limits" in response.text

    def test_job_demo_is_hidden_from_sidebar(self, client: TestClient):
        assert "job_demo" not in client.get("/dev").text


class TestJobConsolePrompt:
    """job_console プロンプトの Enter（暗黙送信）既定ボタン契約."""

    def test_ok_button_precedes_cancel_in_dom(self, client: TestClient):
        """OK が最初の submit ボタン = Enter の既定ボタンになる.

        中止（jc-prompt-no）が DOM 先頭にあると、数値入力後の Enter が
        暗黙送信で中止ボタンを押した扱いになり、計測が勝手に中止される。
        """
        text = client.get("/pasting/dispense_calibration").text

        assert text.index('id="jc-prompt-ok"') < text.index('id="jc-prompt-no"')


class TestPreviewPages:
    """Phase 2: posctrl の preview 専用ページ."""

    def test_camera_preview_page_renders_preview_pane(self, client: TestClient):
        # ストリーム URL は preview.js が動的に装着するため、HTML には
        # preview ペインと overlay 切替（none / crosshair）が出ていればよい
        response = client.get("/posctrl/camera_preview")

        assert response.status_code == 200
        assert "preview-pane" in response.text
        assert "preview.js" in response.text
        assert "crosshair" in response.text

    def test_camera_preview_page_defaults_overlay_to_none(self, client: TestClient):
        """Overlay 既定はページごとに指定でき、camera_preview は素の映像（none）のまま."""
        assert _checked_overlay(client.get("/posctrl/camera_preview").text) == "none"

    def test_copper_detection_page_renders_canny_controls(self, client: TestClient):
        response = client.get("/posctrl/copper_detection")

        assert response.status_code == 200
        assert "canny" in response.text
        assert "設定に保存" in response.text
        # スライダー初期値は machine.toml の pad_align 値（canny_low=81 / canny_high=192）
        assert "81" in response.text
        assert "192" in response.text


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
        ),
    )
    def test_posctrl_job_page_default_overlay(
        self, client: TestClient, feature: str, expected: str
    ):
        """ツアー系（board_tour / orthogonality_test）は十字線を既定 ON で描画する.

        ツアーは十字線を基準に位置を目視合わせするため、ユーザーがラジオを操作せずとも
        十字線が出ている必要がある。camera_calibration は素の映像を見るページなので none。
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

    def test_camera_calibration_has_no_crop_input(self, client: TestClient):
        """クロップ編集 UI は settings ページへ統一済み。このページには置かない.

        ユーザー追加指示: 'crop 値の編集 UI を settings ページのみに置く形に統一'。
        当初計画（machine.toml 連動の即保存フォームをこのページへ）は撤回された。
        """
        text = client.get("/posctrl/camera_calibration").text

        assert "data-machine-key" not in text
        assert 'id="camera-crop-settings"' not in text
        assert "クロップ設定" not in text

    def test_camera_calibration_renders_saved_square_size_default(
        self, client: TestClient, appstate: AppState
    ):
        """入力途中の即保存値（param-defaults 相当）が次回描画の default に反映される."""
        appstate.merge_job_param_defaults("camera_calibration", {"square_size": 3.0})

        text = client.get("/posctrl/camera_calibration").text

        assert 'value="3.0"' in text

    @pytest.mark.parametrize("feature", ("board_tour", "orthogonality_test"))
    def test_tour_pages_render_tolerance_field(self, client: TestClient, feature: str):
        assert "tolerance" in client.get(f"/posctrl/{feature}").text

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

    def test_generate_grid_pcb_renders_form_without_preview(self, client: TestClient):
        """generate_grid_pcb はカメラ非依存の生成ジョブ（job.html、preview なし）."""
        text = client.get("/posctrl/generate_grid_pcb").text

        assert "job-console" in text
        assert "job-form" in text
        assert "preview-pane" not in text
        for name in ("size", "divisions", "pad_size"):
            assert name in text


PASTING_JOB_FEATURES = (
    "paste_solder",
    "height_plane",
    "loading",
    "dispense_calibration",
    "generate_rect_pcb",
    "toolhead_offset",
)

# カメラを使うジョブのみ preview ペインを持つ（JobDefinition.provides_preview）
PASTING_PREVIEW_FEATURES = ("paste_solder", "height_plane", "toolhead_offset")

# ローディングボタン UI を持つ feature（JobDefinition.loading_param）
# dispense_calibration はメニュー段階で押出/吸引（プライム）に使う
PASTING_LOADING_FEATURES = (
    "paste_solder",
    "loading",
    "dispense_calibration",
    "toolhead_offset",
)


class TestPastingJobPages:
    """Phase 5: pasting のジョブページ（計画書 webui-phase5.md「templates /
    static」節 + spec §10 pasting 表）."""

    @pytest.mark.parametrize("feature", PASTING_JOB_FEATURES)
    def test_pasting_job_page_renders_console_and_form(
        self, client: TestClient, feature: str
    ):
        response = client.get(f"/pasting/{feature}")

        assert response.status_code == 200
        assert "job-console" in response.text
        assert "job-form" in response.text
        # data-job-name 等でページのジョブ名が宣言される
        assert feature in response.text

    def test_paste_solder_renders_auto_threshold_inputs(self, client: TestClient):
        """はんだ塗布ページに auto しきい値の即保存フォームが現在値付きで出る.

        線塗布縦横比・面塗布短辺倍率の 2 フィールドを machine 全体設定として描画する。
        """
        text = client.get("/pasting/paste_solder").text

        # machine 全体設定の即保存フォーム（settings.js が data-machine-settings に bind）
        assert "paste-auto-thresholds" in text
        assert "data-machine-settings" in text
        assert 'data-endpoint="/api/settings/machine"' in text
        assert "settings.js" in text
        # 2 フィールドが name（＝設定キー）とラベル付きで描画される
        assert "paste_dispenser.auto_line_aspect_ratio" in text
        assert "paste_dispenser.auto_area_short_side_factor" in text
        assert "Auto線塗布しきい縦横比" in text
        assert "Auto面塗布しきい短辺倍率" in text

    @pytest.mark.parametrize("feature", PASTING_PREVIEW_FEATURES)
    def test_camera_jobs_render_preview_pane_without_overlay_switch(
        self, client: TestClient, feature: str
    ):
        """Preview ペインあり・overlay 切替なし（固定 none。spec §10 pasting）."""
        text = client.get(f"/pasting/{feature}").text

        assert "preview-pane" in text
        assert "crosshair" not in text  # overlay 切替は出さない

    @pytest.mark.parametrize(
        "feature",
        (
            "loading",
            "generate_rect_pcb",
        ),
    )
    def test_non_camera_jobs_have_no_preview_pane(
        self, client: TestClient, feature: str
    ):
        assert "preview-pane" not in client.get(f"/pasting/{feature}").text

    def test_generate_rect_pcb_renders_param_form_fields(self, client: TestClient):
        text = client.get("/pasting/generate_rect_pcb").text

        assert "preview-pane" not in text
        assert "loading-controls" not in text
        for name in ("width", "height"):
            assert name in text

    @pytest.mark.parametrize("feature", ("paste_solder", "loading", "toolhead_offset"))
    def test_loading_jobs_render_loading_controls_with_stage_contract(
        self, client: TestClient, feature: str
    ):
        """ローディング段階のジョブは data-loading-stage を LOADING_STAGE と一致させる."""
        text = client.get(f"/pasting/{feature}").text

        assert "loading-controls" in text
        assert 'data-loading-stage="ローディング"' in text
        # loading_default（該当 ParamSpec の既定値）が量入力に入る
        assert _loading_amount_input(0.1) in text

    def test_dispense_calibration_loading_controls_use_menu_and_loading_stage(
        self, client: TestClient
    ):
        """dispense_calibration の loading_controls はメニュー段階と ① ローディング段階で有効化する.

        メニュー段階のプライム（押出/吸引）に加え、① rotations_per_ul の専用ローディング段階
        （"ローディング"）でもボタンを使うため、data-loading-stage はカンマ区切りで両方を持つ。
        """
        text = client.get("/pasting/dispense_calibration").text

        assert "loading-controls" in text
        assert 'data-loading-stage="キャリブレーションメニュー,ローディング"' in text

    def test_loading_page_renders_rotation_controls_and_mass_calibration(
        self, client: TestClient
    ):
        """ローディング画面は体積/回転コントロール + 質量キャリブレーション表を持つ.

        質量キャリブは初期 rotations_per_ul 等をゼロから設定するブートストラップ用。 既存値を線引きで補正する
        dispense_calibration とは用途が別なので併存させる。
        """
        text = client.get("/pasting/loading").text

        assert 'type="hidden" id="param-amount"' in text
        assert "体積ローディング量" not in text
        assert "回転ローディング回転数" not in text
        assert "回転ローディング角速度" not in text
        assert "回転ローディング角加速度" not in text
        assert "loading-control-section" in text
        assert "体積" in text
        assert "回転" in text
        assert 'id="lc-rotations"' in text
        assert 'id="lc-rate"' in text
        assert 'id="lc-accel"' in text
        assert 'id="lc-extrude-rotations"' in text
        assert 'id="lc-suck-rotations"' in text
        # 質量キャリブレーション表（ブートストラップ用）はローディング画面に残す
        assert 'id="loading-mass-calibration"' in text
        assert 'id="lc-mass-mg"' in text
        assert 'id="lc-rotations-per-ul"' in text
        assert 'id="lc-apply-rotations-per-ul"' in text
        assert 'data-job-names="loading"' in text
        for value in ('value="5.0"', 'value="0.5"'):
            assert value in text

    def test_loading_page_renders_optional_start_position(self, client: TestClient):
        text = client.get("/pasting/loading").text

        assert "実行すると全軸をホーミング" in text
        assert "ローディング位置（任意）" in text
        assert "空欄の軸はホーミング後の位置を維持します" in text
        for axis in ("x", "y", "z"):
            assert f'id="param-position_{axis}"' in text
        assert text.count('data-param-optional="true"') == 3

    def test_loading_page_renders_saved_loading_defaults(
        self, client: TestClient, appstate: AppState
    ):
        appstate.merge_job_param_defaults(
            "loading",
            {"amount": 0.2, "rotations": 6.0, "rate": 1.5, "accel": 2.5},
        )

        text = client.get("/pasting/loading").text

        assert _loading_amount_input(0.2) in text
        for value in ('value="6.0"', 'value="1.5"', 'value="2.5"'):
            assert value in text

    @pytest.mark.parametrize(
        "feature", ("paste_solder", "dispense_calibration", "toolhead_offset")
    )
    def test_non_loading_pages_do_not_render_rotation_controls(
        self, client: TestClient, feature: str
    ):
        text = client.get(f"/pasting/{feature}").text

        assert 'id="lc-rotations"' not in text
        assert "loading-mass-calibration" not in text

    @pytest.mark.parametrize("feature", ("height_plane", "generate_rect_pcb"))
    def test_non_loading_jobs_have_no_loading_controls(
        self, client: TestClient, feature: str
    ):
        assert "loading-controls" not in client.get(f"/pasting/{feature}").text

    def test_dispense_calibration_renders_param_form_and_menu(self, client: TestClient):
        """銅板/線共通/②/③ のパラメータフォーム + ①②③/全実行/終了メニューが出る."""
        text = client.get("/pasting/dispense_calibration").text

        for name in (
            "board_width",
            "board_height",
            "tolerance",
            "line_length",
            "line_count",
            "removal_z_offset",
            "rate_min",
            "rate_max",
            "rate_divisions",
            "speed_min",
            "speed_max",
            "speed_divisions",
        ):
            assert name in text
        # パラメータは固定土台/線共通/②/③ のセクション（fieldset）に分かれて表示される
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

    def test_dispense_calibration_renders_saved_param_defaults(
        self, client: TestClient, appstate: AppState
    ):
        appstate.merge_job_param_defaults(
            "dispense_calibration",
            {"board_width": 30.0, "line_count": 8, "speed_max": 12.0},
        )

        text = client.get("/pasting/dispense_calibration").text

        for value in ('value="30.0"', 'value="8"', 'value="12.0"'):
            assert value in text

    def test_toolhead_offset_renders_param_form_fields(self, client: TestClient):
        text = client.get("/pasting/toolhead_offset").text

        for name in (
            "tolerance",
            "dispense_amount",
            "loading_amount",
            "lift_height",
            "paste_diameter_min",
            "paste_diameter_max",
            "point_count",
            "point_spacing",
            "edge_margin",
        ):
            assert name in text

    def test_toolhead_offset_renders_saved_param_defaults(
        self, client: TestClient, appstate: AppState
    ):
        appstate.merge_job_param_defaults(
            "toolhead_offset",
            {"point_count": 7, "point_spacing": 6.25, "edge_margin": 4.75},
        )

        text = client.get("/pasting/toolhead_offset").text

        for value in ('value="7"', 'value="6.25"', 'value="4.75"'):
            assert value in text

    def test_loading_mass_calibration_is_not_sidebar_feature(self, client: TestClient):
        assert "loading-mass-calibration" not in client.get("/pasting").text


class TestJobDefinitionDrivenContext:
    """Preview / ローディング UI は JobDefinition から導出する（MR2）.

    以前は pages.py の 3 つのハードコード辞書と `if tab == "pasting"` 特別扱いが正
    だった。preview フレームの提供有無と progress_stage 文字列は backend の事実なので
    ジョブ定義側へ移し、ページはそれを読むだけにした（`/api/jobs` と同じ値になる）。
    """

    @pytest.mark.parametrize("feature", PASTING_JOB_FEATURES)
    def test_preview_pane_presence_matches_provides_preview(
        self, client: TestClient, app: FastAPI, feature: str
    ):
        definition = app.state.catalog.get(feature)

        text = client.get(f"/pasting/{feature}").text

        assert ("preview-pane" in text) is definition.provides_preview

    @pytest.mark.parametrize("feature", PASTING_JOB_FEATURES)
    def test_loading_controls_derive_stage_and_default_from_definition(
        self, client: TestClient, app: FastAPI, feature: str
    ):
        """Loading UI の段階と既定値が定義側の値と一致する.

        `loading.html` / `dispense_calibration.html` は loading_controls を
        `show_loading_controls` で gate せず無条件 include するため、この 2 feature では
        `"loading-controls" in text` が定数 True になる（検出力ゼロ）。段階文字列と
        `#lc-amount` の value を見て、4 feature すべてで定義との一致を確かめる。
        """
        definition = app.state.catalog.get(feature)

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

    def test_probe_guide_is_not_a_job_page(self, client: TestClient):
        text = client.get("/pasting/probe_guide").text

        assert "job-console" not in text
        assert "job-form" not in text

    def test_probe_guide_listed_in_pasting_sidebar(self, client: TestClient):
        assert "probe_guide" in client.get("/pasting").text

    def test_probe_gnd_down_adjust_is_removed(self, client: TestClient):
        assert "probe_gnd_down_adjust" not in client.get("/pasting").text
        assert client.get("/pasting/probe_gnd_down_adjust").status_code == 404


class TestNozzleCapPage:
    """ノズルキャップ位置の設定ページ（nozzle-cap-parking 計画書「WebUI」節）.

    ジョブではない静的な設定ページ（非ジョブテンプレート）。現在値が未記録 （repo fixture に [nozzle_cap]
    なし）のときは「未記録」を表示する。
    """

    def test_nozzle_cap_listed_in_pasting_sidebar(self, client: TestClient):
        text = client.get("/pasting").text

        assert "nozzle_cap" in text
        assert "ノズルキャップ位置の設定" in text

    def test_page_renders_record_button_without_job_console(self, client: TestClient):
        response = client.get("/pasting/nozzle_cap")

        assert response.status_code == 200
        text = response.text
        assert "job-console" not in text
        assert "job-form" not in text
        assert "nozzle_cap.js" in text
        assert "記録" in text

    def test_unrecorded_cap_shows_placeholder(self, client: TestClient):
        text = client.get("/pasting/nozzle_cap").text

        assert "未記録" in text

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


class TestMachineControlCapButton:
    """マシン操作パネルの「キャップ位置に移動」ボタン（paste マシン限定表示）.

    nozzle-cap-parking 計画書「WebUI」節: machine_control.html は machine_type
    == "paste" のときのみ #mc-move-to-cap を出す。machine_type が読めない config でも
    ページは 500 にならずボタンを隠す（state.machine_type() は broad except → None）。
    """

    def test_paste_machine_renders_move_to_cap_button(self, client: TestClient):
        response = client.get("/posctrl")

        assert response.status_code == 200
        assert "mc-move-to-cap" in response.text

    def test_missing_machine_type_hides_button_without_error(
        self, client: TestClient, config_dir: Path
    ):
        path = config_dir / "machine.toml"
        lines = [
            line
            for line in path.read_text(encoding="utf-8").splitlines(keepends=True)
            if not line.startswith("machine_type")
        ]
        path.write_text("".join(lines), encoding="utf-8")

        response = client.get("/posctrl")

        assert response.status_code == 200
        assert "mc-move-to-cap" not in response.text


class TestPasteSolderPadEditor:
    """Phase 4: paste_solder の pad 編集フロント UI（計画書 Phase 4
    「templates / static」節）.

    paste_solder は専用テンプレ（pasting/paste_solder.html）に切り替わり、
    pad editor の DOM フックを持ちつつ、従来どおり job-console / job-form /
    preview ペイン / loading_controls も備える。他 pasting feature は
    pasting/job.html のまま回帰しない（pad editor を持たない）。
    """

    def test_paste_solder_renders_pad_editor_hooks(self, client: TestClient):
        text = client.get("/pasting/paste_solder").text

        # SVG ビューア / 選択ツールバー / 階層表コンテナ / スクリプト
        assert 'id="pad-viewer"' in text
        assert 'id="pad-enable-selected"' in text
        assert 'id="pad-disable-selected"' in text
        assert "全有効" in text
        assert "全無効" in text
        assert 'id="pad-table"' in text
        assert 'id="pad-editor-empty"' in text
        assert 'id="pad-outline-size"' not in text
        assert 'id="pad-export-config"' in text
        assert 'id="pad-import-config"' in text
        assert 'id="pad-calculate-route"' in text
        assert 'id="pad-calculate-fill-path"' in text
        assert 'id="pad-route-status"' not in text
        assert "面積あたりのペースト量" in text
        assert "pad_editor/index.js" in text
        # レイヤ切替（Top/Bottom）
        assert 'name="pad-layer"' in text

    def test_paste_solder_keeps_job_chrome(self, client: TestClient):
        """専用テンプレでも job-console / job-form / preview / loading は維持."""
        text = client.get("/pasting/paste_solder").text

        assert "job-console" in text
        assert "job-form" in text
        assert "preview-pane" in text  # JobDefinition.provides_preview
        assert "pasting-preview-panel" in text
        assert "crosshair" not in text  # overlay 切替は出さない
        assert "loading-controls" in text  # JobDefinition.loading_param

    @pytest.mark.parametrize(
        "feature",
        ("height_plane", "loading", "toolhead_offset"),
    )
    def test_other_pasting_features_have_no_pad_editor(
        self, client: TestClient, feature: str
    ):
        """Pad editor は paste_solder 専用。他 feature は pasting/job.html のまま."""
        text = client.get(f"/pasting/{feature}").text

        assert "pad-viewer" not in text
        assert "pad_editor/index.js" not in text


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


class TestPnpPlaceholder:
    """Pnp タブはプレースホルダのみ（Phase 5 で確認。spec §10 pnp）."""

    def test_pnp_renders_placeholder_with_empty_sidebar(self, client: TestClient):
        response = client.get("/pnp")

        assert response.status_code == 200
        assert "機能を選択" in response.text
        # サイドバーに feature リンクが無い（他タブの feature 名が出ない）
        for feature in PASTING_JOB_FEATURES + POSCTRL_JOB_FEATURES:
            assert feature not in response.text
