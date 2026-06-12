"""`webui.routers.pages` の仕様テスト.

計画書「routers」節 + spec §10:

- `GET /` → `/posctrl` へ 307 リダイレクト
- 4 タブ + 既知 feature + `/settings` が 200
- 全ページ共通の chrome（E-STOP・マシン操作パネル・mainsail console リンク）
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

- pasting 6 feature ページは全て job-console + job-form
- preview ペイン（overlay 切替なし）は paste_solder / height_plane /
  toolhead_offset のみ
- loading_controls（data-loading-stage="ローディング"）は paste_solder /
  loading / flow_calibration / toolhead_offset のみ
- pnp はプレースホルダのみ（サイドバー空 + 「機能を選択」）
"""

import attrs
import pytest
from fastapi.testclient import TestClient

from webui.app import create_app
from webui.settings import Settings

TABS = ["dev", "pasting", "pnp", "posctrl"]


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
        assert "machine-control" in response.text
        assert "マシン選択:" in response.text
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

    def test_mainsail_link_follows_request_host_when_unset(
        self, webui_settings: Settings
    ):
        """PCBASM_MAINSAIL_URL 未設定時はページ閲覧元のホスト名に追従する."""
        app = create_app(attrs.evolve(webui_settings, mainsail_url=None))
        with TestClient(app) as client:
            text = client.get("/posctrl").text

        assert 'href="http://testserver"' in text

    def test_known_feature_page_renders(self, client: TestClient):
        response = client.get("/posctrl/reference_point_setup")

        assert response.status_code == 200

    def test_settings_page_renders(self, client: TestClient):
        response = client.get("/settings")

        assert response.status_code == 200

    def test_settings_page_groups_fields_by_section(self, client: TestClient):
        """設定項目はセクション単位の階層表示（settings-group）でまとまる."""
        text = client.get("/settings").text

        assert "settings-group" in text
        for section_label in (
            "ペーストディスペンサー",
            "ペーストディスペンサー / パッド位置合わせ",
            "プローブ",
            "基準点",
            "カメラ",
        ):
            assert section_label in text
        # モーション設定（printer.cfg）は Mainsail 直編集に移行し画面から削除済み
        assert "モーション設定" not in text

    def test_unknown_tab_returns_404(self, client: TestClient):
        assert client.get("/no-such-tab").status_code == 404

    def test_unknown_feature_returns_404(self, client: TestClient):
        assert client.get("/posctrl/no-such-feature").status_code == 404


DEV_JOB_FEATURES = (
    "extract_pcb",
    "fill_path_simulate",
    "generate_grid_pcb",
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

    def test_fill_path_simulate_renders_param_form_fields(self, client: TestClient):
        text = client.get("/dev/fill_path_simulate").text

        for name in (
            "nozzle_diameter",
            "layer",
            "bead_width_factor",
            "overlap",
            "boundary_margin",
        ):
            assert name in text

    def test_klipper_status_page_renders_gcode_box_and_limits(self, client: TestClient):
        response = client.get("/dev/klipper_status")

        assert response.status_code == 200
        assert "gcode" in response.text
        assert "limits" in response.text

    def test_job_demo_is_hidden_from_sidebar(self, client: TestClient):
        assert "job_demo" not in client.get("/dev").text


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

    def test_camera_calibration_renders_param_form_fields(self, client: TestClient):
        text = client.get("/posctrl/camera_calibration").text

        for name in ("square_size", "crop_width", "crop_height"):
            assert name in text

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


PASTING_JOB_FEATURES = (
    "paste_solder",
    "height_plane",
    "loading",
    "flow_calibration",
    "toolhead_offset",
    "probe_gnd_down_adjust",
)

# カメラを使うジョブのみ preview ペインを持つ（計画書 _PASTING_PREVIEW）
PASTING_PREVIEW_FEATURES = ("paste_solder", "height_plane", "toolhead_offset")

# ローディングボタン UI を持つ feature（計画書 _PASTING_LOADING_PARAM）
PASTING_LOADING_FEATURES = (
    "paste_solder",
    "loading",
    "flow_calibration",
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

    @pytest.mark.parametrize("feature", PASTING_PREVIEW_FEATURES)
    def test_camera_jobs_render_preview_pane_without_overlay_switch(
        self, client: TestClient, feature: str
    ):
        """Preview ペインあり・overlay 切替なし（固定 none。spec §10 pasting）."""
        text = client.get(f"/pasting/{feature}").text

        assert "preview-pane" in text
        assert "crosshair" not in text  # overlay 切替は出さない

    @pytest.mark.parametrize(
        "feature", ("loading", "flow_calibration", "probe_gnd_down_adjust")
    )
    def test_non_camera_jobs_have_no_preview_pane(
        self, client: TestClient, feature: str
    ):
        assert "preview-pane" not in client.get(f"/pasting/{feature}").text

    @pytest.mark.parametrize("feature", PASTING_LOADING_FEATURES)
    def test_loading_jobs_render_loading_controls_with_stage_contract(
        self, client: TestClient, feature: str
    ):
        """Loading_controls の data-loading-stage は LOADING_STAGE と一致させる."""
        text = client.get(f"/pasting/{feature}").text

        assert "loading-controls" in text
        assert 'data-loading-stage="ローディング"' in text
        assert 'value="0.1"' in text  # loading_default（該当 ParamSpec の既定値）

    @pytest.mark.parametrize("feature", ("height_plane", "probe_gnd_down_adjust"))
    def test_non_loading_jobs_have_no_loading_controls(
        self, client: TestClient, feature: str
    ):
        assert "loading-controls" not in client.get(f"/pasting/{feature}").text

    def test_flow_calibration_renders_param_form_fields(self, client: TestClient):
        text = client.get("/pasting/flow_calibration").text

        for name in ("rotations", "rate", "accel", "load_amount"):
            assert name in text

    def test_toolhead_offset_renders_param_form_fields(self, client: TestClient):
        text = client.get("/pasting/toolhead_offset").text

        for name in (
            "tolerance",
            "dispense_amount",
            "loading_amount",
            "lift_height",
            "paste_diameter_min",
            "paste_diameter_max",
        ):
            assert name in text


class TestPnpPlaceholder:
    """Pnp タブはプレースホルダのみ（Phase 5 で確認。spec §10 pnp）."""

    def test_pnp_renders_placeholder_with_empty_sidebar(self, client: TestClient):
        response = client.get("/pnp")

        assert response.status_code == 200
        assert "機能を選択" in response.text
        # サイドバーに feature リンクが無い（他タブの feature 名が出ない）
        for feature in PASTING_JOB_FEATURES + POSCTRL_JOB_FEATURES:
            assert feature not in response.text
