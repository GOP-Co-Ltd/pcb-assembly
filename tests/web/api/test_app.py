"""`webui.app.create_app` の仕様テスト.

計画書「`src/webui/app.py` / `__main__.py`」節:

- create_app(settings) で注入 Settings のアプリが起動する
- BusyError → 409 JSON の exception handler が登録される
- /static 配下の静的ファイル配信

MR1 追記（計画書 web-api-ui-split.md「MR1」節）:

- JobManager と HTTP 経路は同一の BoardSettingsStore を共有する
  （別インスタンスだと更新ロックが効かず、ジョブ実行中の pad 編集が消える）
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.helpers import wait_until
from webui.board_settings import BoardSettingsStore
from webui.jobs.catalog import JobDefinition
from webui.jobs.context import JobContext
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
