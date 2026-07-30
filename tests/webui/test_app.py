"""`webui.app.create_app` の仕様テスト.

計画書「`src/webui/app.py` / `__main__.py`」節:

- create_app(settings) で注入 Settings のアプリが起動する
- BusyError → 409 JSON の exception handler が登録される
- /static 配下の静的ファイル配信

webui-audio-output 計画書「`src/webui/app.py`」節が追記契約:

- create_app(audio_player=...) で注入したプレイヤーを lifespan 終了時に
  1 回だけ close する（ジョブ join の後に閉じる）
"""

from fastapi.testclient import TestClient

from tests.helpers import FakeAudioPlayer
from webui.app import create_app
from webui.settings import Settings
from webui.state import AppState


class TestCreateApp:
    """アプリケーションファクトリ."""

    def test_app_starts_and_serves_state(self, client: TestClient):
        response = client.get("/api/state")

        assert response.status_code == 200

    def test_busy_error_handler_returns_409_json(
        self, client: TestClient, appstate: AppState
    ):
        with appstate.machine_lock("pytest-job"):
            response = client.put(
                "/api/pcb-file", json={"path": "boards/sample.kicad_pcb"}
            )

        assert response.status_code == 409
        assert response.headers["content-type"].startswith("application/json")
        assert "pytest-job" in response.text

    def test_static_css_is_served(self, client: TestClient):
        response = client.get("/static/app.css")

        assert response.status_code == 200
        assert "text/css" in response.headers["content-type"]

    def test_lifespan_closes_injected_audio_player_once(self, webui_settings: Settings):
        player = FakeAudioPlayer()
        app = create_app(webui_settings, audio_player=player)

        with TestClient(app) as client:
            assert client.get("/api/state").status_code == 200

        assert player.close_calls == 1
