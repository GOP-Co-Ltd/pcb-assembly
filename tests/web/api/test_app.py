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

設定エラー（degraded）の契約:

- machine.toml が検証に通らなくてもプロセスは起動し、理由を `/api/state` の config_error に出す
- ジョブの開始は拒否する。設定・設定履歴・バックアップと、[klipper] が読めれば緊急停止は使える
- 毎回ディスクから読むので、手編集は再起動なしで次のジョブと config_error に効く

起動時の移行（pcbasm.migrate）の契約:

- 設定を読む前に移行し、移行したことを `/api/state` の config_notice に出す
- 移行できなければ元のファイルに触れずに degraded で起動し、理由を config_error に出す
"""

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcbasm.hal.fake import FakeAudioPlayer, FakeKlipper
from pcbasm.paths import PROJECT_ROOT
from tests.support.polling import wait_until
from web.api.app import create_app
from web.api.board_settings import BoardSettingsStore
from web.api.jobs.catalog import JobDefinition
from web.api.jobs.context import JobContext
from web.api.settings import Settings
from web.api.state import AppState

MIGRATION_BEFORE = (
    PROJECT_ROOT / "tests" / "pcbasm" / "migrate" / "machine-before-M.toml"
)

# data/testing/config/machine.toml の 1 行（lift_height は正の値が必要）
_LIFT_HEIGHT = "lift_height = 2.0"


def _edit(config_dir: Path, old: str, new: str) -> None:
    """machine.toml を ssh で手編集したのと同じ書き換えをする."""
    path = config_dir / "machine.toml"
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


@pytest.fixture
def broken_config(config_dir: Path) -> Path:
    """create_app より前に machine.toml を検証に通らない値にする（config_dir を返す）."""
    _edit(config_dir, _LIFT_HEIGHT, "lift_height = 0.0")
    return config_dir


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
                run=run,
                uses_machine=False,
            )
        )

        response = client.post("/api/jobs/board-store-probe")

        assert response.status_code == 201, response.text
        wait_until(lambda: bool(captured))
        assert captured[0] is app.state.board_store


class TestDegraded:
    """設定エラーでも落ちずに起動し、WebUI から直せる."""

    def test_starts_and_reports_the_reason(
        self, broken_config: Path, client: TestClient
    ):
        state = client.get("/api/state").json()

        assert "lift_height" in state["config_error"]
        assert client.get("/api/machine-info").status_code == 200

    def test_refuses_to_start_a_job(
        self, broken_config: Path, app: FastAPI, client: TestClient
    ):
        started: list[bool] = []
        app.state.catalog.register(
            JobDefinition(
                name="degraded-probe",
                label="degraded の確認",
                run=lambda ctx: started.append(True),
                uses_machine=False,
            )
        )

        response = client.post("/api/jobs/degraded-probe")

        assert response.status_code == 503
        assert "lift_height" in response.json()["detail"]
        assert started == []

    def test_settings_history_and_backup_stay_usable(
        self, broken_config: Path, client: TestClient
    ):
        assert client.get("/api/settings/history").status_code == 200
        assert client.get("/api/backup").status_code == 200

        response = client.put(
            "/api/settings/machine",
            json={"values": {"paste_dispenser.motion.lift_height": 2.0}},
        )

        assert response.status_code == 200, response.text
        assert client.get("/api/state").json()["config_error"] is None

    def test_emergency_stop_still_reaches_klipper(
        self,
        broken_config: Path,
        client: TestClient,
        mount_klipper: Callable[[FakeKlipper], None],
    ):
        klipper = FakeKlipper()
        mount_klipper(klipper)

        assert client.post("/api/emergency-stop").status_code == 200
        assert klipper.sent_lines == ["M112"]


class TestMigrationAtStartup:
    """設定を読む前に machine.toml を移行する."""

    def test_migrates_the_old_layout_and_reports_it(
        self, config_dir: Path, webui_settings: Settings
    ):
        path = config_dir / "machine.toml"
        shutil.copy(MIGRATION_BEFORE, path)

        with TestClient(create_app(webui_settings)) as client:
            state = client.get("/api/state").json()

        assert state["config_error"] is None
        assert "machine-pre-M.toml" in state["config_notice"]
        assert (config_dir / "history" / "machine-pre-M.toml").is_file()
        assert "[paste_dispenser.motion]" in path.read_text()

    def test_current_layout_starts_without_notice(self, client: TestClient):
        assert client.get("/api/state").json()["config_notice"] is None

    @pytest.mark.parametrize(
        ("header", "inserted", "reason"),
        [
            ("[camera]", "[broken\n", "TOML として読めません"),
            (
                "[camera]",
                "[paste_dispenser.pad_align]\ncanny_low = 1.0\n\n",
                "途中まで移行済み",
            ),
            (
                "[alignment]",
                "[paste_dispenser.pad_align]\ncanny_low = 1.0\n\n",
                "ほかの節をはさんで",
            ),
        ],
        ids=["broken", "partially-migrated", "interleaved"],
    )
    def test_unmigratable_file_starts_degraded_and_is_left_untouched(
        self,
        config_dir: Path,
        webui_settings: Settings,
        header: str,
        inserted: str,
        reason: str,
    ):
        """``inserted`` を ``header`` の直前に書いた machine.toml で起動する."""
        _edit(config_dir, f"\n{header}\n", f"\n{inserted}{header}\n")
        path = config_dir / "machine.toml"
        original = path.read_text()

        with TestClient(create_app(webui_settings)) as client:
            state = client.get("/api/state").json()

        assert "machine.toml を移行できませんでした" in state["config_error"]
        assert reason in state["config_error"]
        assert path.read_text() == original


class TestReadsFromDiskEveryTime:
    """手編集と書き込みは再起動なしで次の読み込みに効く."""

    def test_config_error_appears_and_disappears_without_restart(
        self, config_dir: Path, client: TestClient
    ):
        assert client.get("/api/state").json()["config_error"] is None

        _edit(config_dir, _LIFT_HEIGHT, "lift_height = 0.0")
        assert "lift_height" in client.get("/api/state").json()["config_error"]

        _edit(config_dir, "lift_height = 0.0", _LIFT_HEIGHT)
        assert client.get("/api/state").json()["config_error"] is None

    def test_hand_edit_takes_effect_on_the_next_job(
        self, config_dir: Path, app: FastAPI, client: TestClient
    ):
        seen: list[float] = []
        app.state.catalog.register(
            JobDefinition(
                name="lift-height-probe",
                label="lift_height の確認",
                run=lambda ctx: seen.append(
                    ctx.machine.paste_dispenser.motion.lift_height
                ),
                uses_machine=False,
            )
        )

        _edit(config_dir, _LIFT_HEIGHT, "lift_height = 2.5")
        response = client.post("/api/jobs/lift-height-probe")

        assert response.status_code == 201, response.text
        wait_until(lambda: bool(seen))
        assert seen == [2.5]
