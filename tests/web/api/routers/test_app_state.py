"""`web.api.routers.app_state` の仕様テスト.

計画書「routers」節:

- GET /api/state — StateResponse の全フィールド

MR2（計画書 docs/plans/web-api-ui-split.md「MR2」節）が追記契約:

- GET /api/machine-info — backend の自己申告（machine_id / machine_type /
  mainsail_url / fb_start / api_version）。到達性プローブも兼ねる
- machine_id は `Settings.hostname` があればそれ、無ければ backend ホストの hostname
  （これがそのまま機体名。machine.toml に表示名の設定は持たない）
- mainsail_url は backend 側で解決した値。リクエストのホスト名には追従しない
  （プロキシ配下で必ず誤るため）。未設定時は `http://{machine_id}.local`
  （machine_id が既にドットを含むなら `.local` を重ねない）
  fake の機体（`Settings.fake`）は実機の Mainsail へ導かないよう空文字を返す
- StateResponse に nozzle_cap を追加。`[paste_dispenser.nozzle_cap]` が x/y/z を揃えていない
  machine.toml でも 200 + null（防御しないと全クライアントのポーリングが 500 する）
"""

from pathlib import Path

import attrs
from fastapi.testclient import TestClient

from web.api.app import create_app
from web.api.machine_config import MachineConfigFile
from web.api.settings import Settings
from web.api.state import AppState


class TestStateApi:
    """GET /api/state."""

    def test_state_reports_all_fields(
        self, client: TestClient, webui_settings: Settings
    ):
        response = client.get("/api/state")

        assert response.status_code == 200
        data = response.json()
        assert data["pcb_file"] is None
        assert data["busy"] is False
        assert data["busy_owner"] is None
        assert data["focus_z"] == -25.0
        assert data["mainsail_url"] == webui_settings.mainsail_url
        assert data["preview_clients"] == 0  # Phase 2: spec §9
        # fixture の machine.toml に [paste_dispenser.nozzle_cap] / [paste_dispenser.nozzle_clean] が無いので未記録
        assert data["nozzle_cap"] is None
        assert data["nozzle_clean"] is None

    def test_state_reports_busy_while_locked(
        self, client: TestClient, appstate: AppState
    ):
        with appstate.machine_lock("pytest-job"):
            data = client.get("/api/state").json()

        assert data["busy"] is True
        assert data["busy_owner"] == "pytest-job"

    def test_state_reports_recorded_nozzle_cap(
        self, client: TestClient, machine_config: MachineConfigFile
    ):
        machine_config.write(
            {"paste_dispenser.nozzle_cap.position": [10.0, 20.0, -3.5]}
        )

        data = client.get("/api/state").json()

        assert data["nozzle_cap"] == {"x": 10.0, "y": 20.0, "z": -3.5}

    def test_partially_recorded_nozzle_cap_reports_the_config_error(
        self, partial_nozzle_cap: Path, client: TestClient
    ):
        """X だけ書かれた `[paste_dispenser.nozzle_cap]` は設定エラー（degraded）.

        全ページの SSR と全クライアントのポーリングが読むので、500 にせず理由を返す。
        """
        response = client.get("/api/state")

        assert response.status_code == 200
        assert response.json()["nozzle_cap"] is None
        assert "x / y / z の 3 つ" in response.json()["config_error"]

    def test_state_reports_recorded_nozzle_clean_with_server_built_label(
        self, client: TestClient, machine_config: MachineConfigFile
    ):
        """表示文字列はサーバーが組んで返す（JS に整形させない）."""
        machine_config.write(
            {
                "paste_dispenser.nozzle_clean.position": [10.0, 20.0, -30.0],
                "paste_dispenser.nozzle_clean.press_depth": 0.4,
            }
        )

        data = client.get("/api/state").json()

        assert data["nozzle_clean"]["label"] == "(10.00, 20.00, -30.00) mm"

    def test_partially_recorded_nozzle_clean_reports_null_without_error(
        self, partial_nozzle_clean: Path, client: TestClient
    ):
        """座標の無い `[paste_dispenser.nozzle_clean]` でも /api/state は 200 + null
        を返す."""
        response = client.get("/api/state")

        assert response.status_code == 200
        assert response.json()["nozzle_clean"] is None


class TestMachineInfoApi:
    """GET /api/machine-info — backend の自己申告（到達性プローブ兼用）."""

    def test_reports_all_fields(self, webui_settings: Settings):
        settings = attrs.evolve(webui_settings, hostname="paste-01")
        with TestClient(create_app(settings)) as client:
            response = client.get("/api/machine-info")

        assert response.status_code == 200
        assert response.json() == {
            "machine_id": "paste-01",
            "mainsail_url": settings.mainsail_url,
            # pcb_browse_start == pcb_browse_root なので初期表示は root
            "fb_start": "",
            # frontend / discovery の互換判定に使う値なので、`web.contract.discovery.API_VERSION`
            # を import せずリテラルで固定する（import すると同語反復になり、うっかりの
            # バンプが無音で通る）。バンプは意図的な契約変更なのでここも同時に更新する
            "api_version": 9,
        }

    def test_mainsail_url_resolves_from_machine_id_not_request_host(
        self, webui_settings: Settings
    ):
        """Mainsail_url 未設定時は machine_id から解決し、リクエスト元には追従しない.

        プロキシ配下（frontend 経由）ではリクエストのホスト名が frontend 機を指すため、
        `request.url.hostname` フォールバックは必ず誤る。machine_id は短いホスト名なので
        LAN の他端末からは mDNS の `*.local` しか引けず、`.local` を付けて返す。
        """
        settings = attrs.evolve(webui_settings, mainsail_url=None, hostname="paste-01")
        with TestClient(create_app(settings)) as client:
            info = client.get("/api/machine-info").json()

        assert info["mainsail_url"] == "http://paste-01.local"
        assert "testserver" not in info["mainsail_url"]

    def test_mainsail_url_does_not_repeat_local_on_a_dotted_machine_id(
        self, webui_settings: Settings
    ):
        """Machine_id が既にドットを含むなら `.local` を重ねない（FQDN 注入・`.local` 注入）."""
        settings = attrs.evolve(
            webui_settings, mainsail_url=None, hostname="paste-01.local"
        )
        with TestClient(create_app(settings)) as client:
            info = client.get("/api/machine-info").json()

        assert info["mainsail_url"] == "http://paste-01.local"

    def test_fake_machine_does_not_link_to_a_real_mainsail(
        self, webui_settings: Settings
    ):
        """Fake の機体は実機と同じ machine_id を名乗るので、そこから Mainsail を導かない."""
        settings = attrs.evolve(
            webui_settings, mainsail_url=None, hostname="paste-01", fake=True
        )
        with TestClient(create_app(settings)) as client:
            info = client.get("/api/machine-info").json()

        assert info["mainsail_url"] == ""

    def test_fb_start_is_relative_to_browse_root(self, webui_settings: Settings):
        settings = attrs.evolve(
            webui_settings, pcb_browse_root=webui_settings.pcb_browse_root.parent
        )
        with TestClient(create_app(settings)) as client:
            info = client.get("/api/machine-info").json()

        assert info["fb_start"] == webui_settings.pcb_browse_start.name
