"""`web.api.app.create_app` の仕様テスト.

計画書「`src/webui/app.py` / `__main__.py`」節:

- create_app(settings) で注入 Settings のアプリが起動する
- BusyError → 409 JSON の exception handler が登録される

MR1 追記（計画書 web-api-ui-split.md「MR1」節）:

- JobManager と HTTP 経路は同一の BoardSettingsStore を共有する
  （別インスタンスだと更新ロックが効かず、ジョブ実行中の pad 編集が消える）

MR4 追記（同「MR4」節）: ``templates`` / ``static`` は frontend (`web.ui`) へ移設した
ため、backend は静的資産を配信しない（ブラウザキャッシュを 1 本で共有するために
``/static`` は frontend が prefix なしで持つ）。

webui-audio-output 計画書「`src/webui/app.py`」節が追記契約（通知音は Pi の ALSA で
鳴らすので backend が所有する）:

- create_app(audio_player=...) で注入したプレイヤーを lifespan 終了時に
  1 回だけ close する（ジョブ join の後に閉じる）
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.helpers import FakeAudioPlayer, wait_until
from web.api.app import create_app
from web.api.board_settings import BoardSettingsStore
from web.api.jobs.catalog import JobDefinition
from web.api.jobs.context import JobContext
from web.api.settings import Settings
from web.api.state import AppState


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

    def test_static_assets_are_not_served_by_backend(self, client: TestClient):
        """静的資産は frontend の所有（backend に二重に置かない）."""
        response = client.get("/static/app.css")

        assert response.status_code == 404

    def test_lifespan_closes_injected_audio_player_once(self, webui_settings: Settings):
        player = FakeAudioPlayer()
        app = create_app(webui_settings, audio_player=player)

        with TestClient(app) as client:
            assert client.get("/api/state").status_code == 200

        assert player.close_calls == 1


class TestBoardStoreSharing:
    """ジョブワーカーと HTTP 経路の BoardSettingsStore 同一性."""

    def test_job_context_gets_the_app_board_store_instance(
        self, app: FastAPI, client: TestClient
    ):
        captured: list[BoardSettingsStore | None] = []

        def run(ctx: JobContext) -> None:
            captured.append(ctx.board_store)

        app.state.catalog.register(
            JobDefinition(
                name="board-store-probe",
                label="board_store 配線の確認",
                tab="dev",
                run=run,
                uses_machine=False,
                hidden=True,
            )
        )

        response = client.post("/api/jobs/board-store-probe")

        assert response.status_code == 201, response.text
        wait_until(lambda: bool(captured))
        assert captured[0] is app.state.board_store
