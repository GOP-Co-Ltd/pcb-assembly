"""Frontend 自身の更新（`/update` と `/api/self-update/**`）の仕様テスト.

計画書「3. frontend」が契約:

- パスは backend と**意図的に変える**（`/m/{id}/api/update/**` は proxy で backend 行き）
- **登録順が致命的**: `machines_api.router` → `update_api.router` → `pages.router`。
  逆にすると `/update` が `/{tab}` キャッチオールに食われて 307 になる
- frontend には操作権が無い。守るのは `expected_head` / flock / `enabled` / `confirm`
  の多層で、**いずれも認証ではない**

モックは使わない（`tests/web/update_support.py` の実 git + スタブ実行ファイル）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import attrs
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.helpers import before_deadline
from tests.web.update_support import UpdateSandbox, make_update_sandbox
from web.selfupdate.runner import UpdateRunner
from web.ui.app import create_app
from web.ui.settings import Settings


@pytest.fixture
def sandbox(tmp_path: Path) -> UpdateSandbox:
    """更新できる状態（origin に 1 commit 待っている）の実リポジトリ."""
    box = make_update_sandbox(tmp_path / "selfupdate")
    box.push()
    return box


@pytest.fixture
def runner(sandbox: UpdateSandbox) -> UpdateRunner:
    """Sandbox のリポジトリとスタブを見る更新ランナー."""
    return UpdateRunner(sandbox.settings)


@pytest.fixture
def update_frontend(
    ui_settings: Settings, backend_app: FastAPI, runner: UpdateRunner
) -> FastAPI:
    """更新ランナーを差し替えた frontend（上流は in-process backend）."""
    return create_app(
        ui_settings,
        transport_factory=lambda _endpoint: httpx.ASGITransport(app=backend_app),
        update_runner=runner,
    )


@pytest.fixture
def update_client(update_frontend: FastAPI) -> Iterator[TestClient]:
    with TestClient(update_frontend) as test_client:
        yield test_client


def _finish(runner: UpdateRunner) -> None:
    """バックグラウンドの更新が終わるまで待つ（ハングはテスト失敗にする）."""
    assert before_deadline(lambda: runner.wait(30.0), what="更新の完了") is True


class TestRouteOrder:
    """`/update` が `/{tab}` キャッチオールに食われない（登録順の回帰）."""

    def test_update_page_is_served_not_redirected(self, update_client: TestClient):
        response = update_client.get("/update", follow_redirects=False)

        assert response.status_code == 200
        assert 'id="update-panel"' in response.text

    def test_machines_api_still_answers_json(self, update_client: TestClient):
        """先に登録した `/api/machines` を壊していない."""
        response = update_client.get("/api/machines")

        assert response.status_code == 200
        assert response.json()["machines"]

    def test_backend_update_api_is_proxied_untouched(self, update_client: TestClient):
        """`/m/{id}/api/update/status` は frontend の更新ではなく backend 行き."""
        response = update_client.get("/m/uitest/api/update/status")

        assert response.status_code == 200
        # 上流 backend が自己申告した hostname（frontend 自身のランナーではない）
        assert response.json()["hostname"] == "uitest"


class TestPage:
    """`/update` ページの中身（値は JS が status API から入れる）."""

    def test_page_targets_the_frontend_update_api(self, update_client: TestClient):
        """Backend 行きの更新 API ではなく frontend 自身のエンドポイントを見る."""
        body = update_client.get("/update").text

        assert 'data-transport="frontend"' in body
        assert 'data-status-url="/api/self-update"' in body

    def test_actions_are_not_control_gated(self, update_client: TestClient):
        """Frontend に操作権は無い。印を付けると control.js が永久に inert にする."""
        body = update_client.get("/update").text

        assert "data-requires-control" not in body.split('id="update-panel"')[1]

    def test_page_needs_no_backend(self, ui_settings: Settings, runner: UpdateRunner):
        """機体が 1 台も居ないホスト（frontend 専用機）でも開ける."""
        app = create_app(attrs.evolve(ui_settings, machines=()), update_runner=runner)
        with TestClient(app) as client:
            assert client.get("/update").status_code == 200


class TestSelfUpdateApi:
    """`/api/self-update/**`（frontend 自身のエンドポイント）."""

    def test_status_reports_the_pending_update_after_a_check(
        self, update_client: TestClient
    ):
        assert update_client.post("/api/self-update/check").status_code == 200

        body = update_client.get("/api/self-update").json()

        assert body["repository"]["behind"] == 1
        assert body["update_available"] is True
        assert body["restart_notice"]

    def test_run_advances_the_repository_and_reports_restarting(
        self, update_client: TestClient, sandbox: UpdateSandbox, runner: UpdateRunner
    ):
        target = sandbox.push()

        response = update_client.post(
            "/api/self-update/run", json={"expected_head": sandbox.head()}
        )

        assert response.status_code == 202
        _finish(runner)
        assert sandbox.head() == target
        assert update_client.get("/api/self-update").json()["run"]["state"] == (
            "restarting"
        )

    def test_stale_expected_head_is_refused(
        self, update_client: TestClient, sandbox: UpdateSandbox
    ):
        before = sandbox.head()

        response = update_client.post(
            "/api/self-update/run", json={"expected_head": "0000000"}
        )

        assert response.status_code == 409
        assert sandbox.head() == before
        assert sandbox.restarts() == []

    def test_disabled_host_returns_403(self, ui_settings: Settings, tmp_path: Path):
        sandbox = make_update_sandbox(tmp_path / "selfupdate", enabled=False)
        sandbox.push()
        app = create_app(ui_settings, update_runner=UpdateRunner(sandbox.settings))

        with TestClient(app) as client:
            response = client.post(
                "/api/self-update/run", json={"expected_head": sandbox.head()}
            )

        assert response.status_code == 403
        assert sandbox.calls() == []

    def test_expected_head_is_required(self, update_client: TestClient):
        assert update_client.post("/api/self-update/run", json={}).status_code == 422

    @pytest.mark.parametrize(
        "extra",
        [{"branch": "attacker"}, {"remote": "https://evil.invalid/x.git"}],
    )
    def test_request_cannot_choose_what_is_pulled(
        self,
        extra: dict[str, str],
        update_client: TestClient,
        sandbox: UpdateSandbox,
        runner: UpdateRunner,
    ):
        """取り込む対象はサーバ側固定（ここが開くと LAN から任意コード実行になる）."""
        target = sandbox.push()

        response = update_client.post(
            "/api/self-update/run", json={"expected_head": sandbox.head(), **extra}
        )

        assert response.status_code == 202
        _finish(runner)
        assert sandbox.head() == target
