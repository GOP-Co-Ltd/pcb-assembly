"""`web.api.routers.update` の HTTP 契約テスト.

計画書「2. backend」の表が契約:

| メソッド | パス | 認可 | 成功 | 失敗 |
| --- | --- | --- | --- | --- |
| GET | /api/update/status | なし | 200 | 常に 200（git 失敗は `error` で返す） |
| POST | /api/update/check | ControlDep | 200 | 423 |
| POST | /api/update/run | ControlDep | 202 | 423 / 409 / 403 |

加えて「**参照先を絶対にリクエストパラメータにしない**」（ブランチ・remote・ref・
`uv` 引数はサーバ側固定）を契約テストで固定する。ここが開くと「LAN から任意コード実行」
に悪化するため、`extra` を拒否する形ではなく「渡しても効かない」ことまで見る。

モックは使わない（`tests/web/update_support.py` の実 git + スタブ実行ファイル）。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

import attrs
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.helpers import FakeAudioPlayer, before_deadline
from tests.web.update_support import UpdateSandbox, make_update_sandbox
from web.api.app import create_app
from web.api.settings import Settings
from web.api.state import AppState
from web.selfupdate.runner import UpdateRunner

ALICE = {"X-Pcbasm-Session": "alice-session", "X-Pcbasm-Client-Name": quote("田中")}
BOB = {"X-Pcbasm-Session": "bob-session", "X-Pcbasm-Client-Name": quote("鈴木")}


@pytest.fixture
def sandbox(tmp_path: Path) -> UpdateSandbox:
    """更新できる状態（origin に 1 commit 待っている）の実リポジトリ."""
    box = make_update_sandbox(tmp_path / "update")
    box.push()
    return box


@contextmanager
def closing_client(
    settings: Settings,
    footprint_root: Path,
    runner: UpdateRunner,
) -> Iterator[TestClient]:
    """使い捨ての backend TestClient（lifespan の終了処理まで走らせる）."""
    app: FastAPI = create_app(
        settings,
        audio_player=FakeAudioPlayer(),
        paste_test_board_footprint_root=footprint_root,
        update_runner=runner,
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def runner(sandbox: UpdateSandbox) -> UpdateRunner:
    """Sandbox のリポジトリとスタブを見る更新ランナー."""
    return UpdateRunner(sandbox.settings)


@pytest.fixture
def update_app(
    webui_settings: Settings,
    paste_test_board_footprint_root: Path,
    runner: UpdateRunner,
) -> FastAPI:
    """更新ランナーを差し替えた backend app."""
    return create_app(
        webui_settings,
        audio_player=FakeAudioPlayer(),
        paste_test_board_footprint_root=paste_test_board_footprint_root,
        update_runner=runner,
    )


@pytest.fixture
def update_client(update_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(update_app) as test_client:
        yield test_client


def _finish(runner: UpdateRunner) -> None:
    """バックグラウンドの更新が終わるまで待つ（ハングはテスト失敗にする）."""
    assert before_deadline(lambda: runner.wait(30.0), what="更新の完了") is True


class TestStatus:
    """GET /api/update/status（誰でも読める。常に 200）."""

    def test_reports_the_pending_update(
        self, update_client: TestClient, sandbox: UpdateSandbox
    ):
        # 更新の有無は fetch しないと見えないので、確認経路を先に通す
        update_client.post("/api/update/check", headers=ALICE)

        response = update_client.get("/api/update/status")

        assert response.status_code == 200
        body = response.json()
        assert body["repository"]["behind"] == 1
        assert body["repository"]["branch"] == "main"
        assert body["update_available"] is True
        assert body["summary"]
        assert body["run"]["state"] == "idle"

    def test_needs_no_control_lease(
        self, update_client: TestClient, sandbox: UpdateSandbox
    ):
        """閲覧はゲートしない（他人が操作権を持っていても状態は読める）."""
        update_client.post("/api/update/check", headers=ALICE)

        assert update_client.get("/api/update/status", headers=BOB).status_code == 200

    def test_stays_200_outside_a_git_repository(
        self,
        webui_settings: Settings,
        paste_test_board_footprint_root: Path,
        tmp_path: Path,
    ):
        """`.git` の無いホストでもページが描けるよう、例外にせず理由を返す."""
        plain = tmp_path / "plain"
        plain.mkdir()
        sandbox = make_update_sandbox(tmp_path / "update")
        runner = UpdateRunner(attrs.evolve(sandbox.settings, repo_root=plain))

        with closing_client(
            webui_settings, paste_test_board_footprint_root, runner
        ) as client:
            response = client.get("/api/update/status")

        assert response.status_code == 200
        assert response.json()["repository"]["error"]
        assert response.json()["update_available"] is False

    def test_restart_notice_is_composed_by_the_server(self, update_client: TestClient):
        """「画面も切れます」等の文言は JS で組まない（webui-thin-wrapper）."""
        body = update_client.get("/api/update/status").json()

        assert body["restart_units"] == ["pcbasm-api.service", "pcbasm-ui.service"]
        assert "pcbasm-api.service pcbasm-ui.service" in body["restart_notice"]


class TestControlGate:
    """変更系は操作権が要る（`ControlDep` は必ず `Depends` として使う）."""

    @pytest.mark.parametrize(
        ("path", "payload"),
        [
            ("/api/update/check", None),
            ("/api/update/run", {"expected_head": "0000000"}),
        ],
    )
    def test_non_holder_gets_423(
        self, path: str, payload: dict[str, str] | None, update_client: TestClient
    ):
        update_client.post("/api/update/check", headers=ALICE)

        response = update_client.post(path, headers=BOB, json=payload)

        assert response.status_code == 423
        assert response.json()["holder"]


class TestRun:
    """POST /api/update/run."""

    def test_accepted_run_returns_202_and_advances_the_repository(
        self, update_client: TestClient, sandbox: UpdateSandbox, runner: UpdateRunner
    ):
        target = sandbox.push()
        before = sandbox.head()

        response = update_client.post(
            "/api/update/run", headers=ALICE, json={"expected_head": before}
        )

        assert response.status_code == 202
        assert response.json()["run_id"]
        _finish(runner)
        assert sandbox.head() == target
        status = update_client.get("/api/update/status").json()
        assert status["run"]["state"] == "restarting"
        assert status["run"]["to_head"] == target

    def test_stale_expected_head_is_refused_with_409(
        self, update_client: TestClient, sandbox: UpdateSandbox
    ):
        before = sandbox.head()

        response = update_client.post(
            "/api/update/run", headers=ALICE, json={"expected_head": "0000000"}
        )

        assert response.status_code == 409
        assert sandbox.head() == before
        assert sandbox.restarts() == []

    def test_busy_machine_is_refused_with_409(
        self, update_client: TestClient, update_app: FastAPI, sandbox: UpdateSandbox
    ):
        """塗布中・ジョブ中は更新しない（全ジョブが装置ロックを取る）."""
        state: AppState = update_app.state.appstate
        before = sandbox.head()

        with state.machine_lock("pytest-job"):
            response = update_client.post(
                "/api/update/run", headers=ALICE, json={"expected_head": before}
            )

        assert response.status_code == 409
        assert sandbox.head() == before
        assert sandbox.restarts() == []

    def test_disabled_installation_returns_403(
        self,
        webui_settings: Settings,
        paste_test_board_footprint_root: Path,
        tmp_path: Path,
    ):
        sandbox = make_update_sandbox(tmp_path / "update", enabled=False)
        sandbox.push()

        with closing_client(
            webui_settings,
            paste_test_board_footprint_root,
            UpdateRunner(sandbox.settings),
        ) as client:
            response = client.post(
                "/api/update/run", headers=ALICE, json={"expected_head": sandbox.head()}
            )

        assert response.status_code == 403
        assert sandbox.calls() == []

    def test_expected_head_is_required(self, update_client: TestClient):
        """`curl` 一発で押せないようにする 2 段階（安全弁 1）."""
        response = update_client.post("/api/update/run", headers=ALICE, json={})

        assert response.status_code == 422


class TestRequestCannotChooseWhatIsPulled:
    """**最重要**: 取り込む対象をリクエストで選べない（任意コード実行にしない）."""

    @pytest.mark.parametrize(
        "extra",
        [
            {"branch": "attacker"},
            {"remote": "https://evil.invalid/x.git"},
            {"ref": "refs/heads/attacker"},
            {"uv_sync_args": ["--all-extras"]},
            {"repo_root": "/etc"},
        ],
    )
    def test_extra_fields_do_not_change_what_is_fetched(
        self,
        extra: dict[str, object],
        update_client: TestClient,
        sandbox: UpdateSandbox,
        runner: UpdateRunner,
    ):
        target = sandbox.push()

        response = update_client.post(
            "/api/update/run",
            headers=ALICE,
            json={"expected_head": sandbox.head(), **extra},
        )

        assert response.status_code == 202
        _finish(runner)
        # 追加フィールドは黙って無視され、サーバ側固定の追従先へ進む
        assert sandbox.head() == target
        assert not [line for line in sandbox.calls() if "--all-extras" in line]

    def test_sync_arguments_stay_server_side(
        self, update_client: TestClient, sandbox: UpdateSandbox, runner: UpdateRunner
    ):
        sandbox.push()

        update_client.post(
            "/api/update/run", headers=ALICE, json={"expected_head": sandbox.head()}
        )
        _finish(runner)

        sync = [line for line in sandbox.calls() if " sync " in line]
        assert sync == ["uv sync --locked --inexact"]
