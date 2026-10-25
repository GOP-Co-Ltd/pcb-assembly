"""更新ページ（`/update`）と frontend 自身の更新（`/api/self-update/**`）の仕様テスト.

契約:

- `/update` は唯一の更新操作の画面で、UI サーバー自身と登録済みの全機体のパネルを並べる
- パスは backend と**意図的に変える**（`/m/{id}/api/update/**` は proxy で backend 行き）
- **登録順が致命的**: `machines_api.router` → `update_api.router` → `pages.router`。
  逆にすると `/update` が `/{suffix:path}` キャッチオールに食われて 307 になる
- frontend には操作権が無い。守るのは `expected_head` / flock / `enabled` / `confirm`
  の多層で、**いずれも認証ではない**

モックは使わない（`tests/support/selfupdate.py` の実 git + スタブ実行ファイル）。
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import attrs
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcbasm.hal.fake import FakeAudioPlayer
from tests.support.polling import before_deadline
from tests.support.selfupdate import UpdateSandbox, make_update_sandbox
from tests.support.web import devices_with_audio
from web.api.app import create_app as create_backend_app
from web.api.env import Env as ApiEnv
from web.selfupdate.runner import UpdateRunner
from web.ui.app import create_app
from web.ui.machines import MachineEndpoint
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
    """`/update` が `/{suffix:path}` キャッチオールに食われない（登録順の回帰）."""

    def test_update_page_is_served_not_redirected(self, update_client: TestClient):
        response = update_client.get("/update", follow_redirects=False)

        assert response.status_code == 200
        assert 'data-testid="update-panel-frontend"' in response.text

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

    def test_ui_server_panel_targets_the_frontend_update_api(
        self, update_client: TestClient
    ):
        """UI サーバーのパネルは、backend 行きではなく frontend 自身のエンドポイントを見る."""
        panel = _panel(update_client.get("/update").text, "frontend")

        assert 'data-transport="frontend"' in panel
        assert 'data-status-url="/api/self-update"' in panel

    def test_machine_panel_targets_that_machine_through_the_proxy(
        self, update_client: TestClient
    ):
        """機体のパネルは proxy 経由でその機体の `/api/update/**` を呼ぶ."""
        panel = _panel(update_client.get("/update").text, "uitest")

        assert 'data-transport="backend"' in panel
        assert 'data-status-url="/m/uitest/api/update/status"' in panel
        assert 'data-check-url="/m/uitest/api/update/check"' in panel
        assert 'data-run-url="/m/uitest/api/update/run"' in panel

    def test_actions_are_not_control_gated(self, update_client: TestClient):
        """このページに操作権バーは無い。印を付けると control.js が永久に inert にする.

        機体の更新の確認は操作権なしで受け、実行は操作権が空いていれば backend が要求で取り、ほかの端末が持っていれば断る。
        """
        body = update_client.get("/update").text

        assert "data-requires-control" not in body.split('data-testid="main-pane"')[1]

    def test_colocated_machine_is_explained(self, update_client: TestClient):
        """同居機は UI サーバーと同じソースなので、どちらのパネルでも両方が更新されると案内する."""
        note = update_client.get("/update").text.split('data-testid="update-colocated"')

        assert len(note) == 2
        assert "uitest" in note[1].split("</p>", 1)[0]

    def test_no_colocation_note_on_a_frontend_only_host(
        self,
        ui_settings: Settings,
        backend_app: FastAPI,
        runner: UpdateRunner,
    ):
        app = create_app(
            attrs.evolve(ui_settings, hostname="frontend-only"),
            transport_factory=lambda _endpoint: httpx.ASGITransport(app=backend_app),
            update_runner=runner,
        )
        with TestClient(app) as client:
            body = client.get("/update").text

        assert 'data-testid="update-panel-uitest"' in body
        assert 'data-testid="update-colocated"' not in body

    def test_each_machine_links_its_backup(self, update_client: TestClient):
        """UI サーバーを更新する前に、機体ごとのバックアップ zip を同じ画面から取れる."""
        body = update_client.get("/update").text

        assert 'href="/m/uitest/api/backup"' in body
        response = update_client.get("/m/uitest/api/backup")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"

    def test_update_only_machine_has_no_backup_link(
        self, ui_settings: Settings, runner: UpdateRunner
    ):
        """更新専用の機体も更新できるが、バックアップは中継しない（押しても 409 になる）."""
        old = MachineEndpoint(
            machine_id="old", host="127.0.0.1", port=1, update_only=True
        )
        app = create_app(
            attrs.evolve(ui_settings, machines=(old,)), update_runner=runner
        )
        with TestClient(app) as client:
            body = client.get("/update").text

        assert 'data-status-url="/m/old/api/update/status"' in body
        assert 'data-testid="update-only-old"' in body
        assert "/m/old/api/backup" not in body

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


class TestUpdateNotice:
    """`GET /api/update-notice` — 全ページのトップバーが読む更新通知.

    通知はページを配信している UI サーバー自身と、表示中の機体 backend の両方を見る。
    バッジの見出し・詳細・遷移先はすべてサーバが組む（`webui-thin-wrapper`）。

    通知は補助情報なので、**backend に到達できなくても 200 を返す**。ここで 5xx に
    すると、機体が落ちている間じゅうトップバーがエラーを出し続ける。
    """

    @pytest.fixture
    def sandbox(self, tmp_path: Path) -> UpdateSandbox:
        """Frontend 側は既定で最新（通知が出ない状態）にしておく."""
        return make_update_sandbox(tmp_path / "selfupdate")

    @pytest.fixture
    def machine_sandbox(self, tmp_path: Path) -> UpdateSandbox:
        """機体 backend 側の実リポジトリ（frontend 側とは別の作業ツリー）."""
        return make_update_sandbox(tmp_path / "machine")

    @pytest.fixture
    def machine_runner(self, machine_sandbox: UpdateSandbox) -> UpdateRunner:
        return UpdateRunner(machine_sandbox.settings)

    @contextmanager
    def _client(
        self,
        ui_settings: Settings,
        backend_settings: ApiEnv,
        footprint_root: Path,
        runner: UpdateRunner,
        machine_runner: UpdateRunner,
    ) -> Iterator[TestClient]:
        """Frontend も機体 backend も sandbox のリポジトリを見る構成を組む."""
        backend = create_backend_app(
            backend_settings,
            devices=devices_with_audio(backend_settings, FakeAudioPlayer()),
            paste_test_board_footprint_root=footprint_root,
            update_runner=machine_runner,
        )
        app = create_app(
            ui_settings,
            transport_factory=lambda _endpoint: httpx.ASGITransport(app=backend),
            update_runner=runner,
        )
        try:
            with TestClient(app) as test_client:
                yield test_client
        finally:
            backend.state.backend.close()

    @pytest.fixture
    def notice_client(
        self,
        ui_settings: Settings,
        backend_settings: ApiEnv,
        paste_test_board_footprint_root: Path,
        runner: UpdateRunner,
        machine_runner: UpdateRunner,
    ) -> Iterator[TestClient]:
        with self._client(
            ui_settings,
            backend_settings,
            paste_test_board_footprint_root,
            runner,
            machine_runner,
        ) as test_client:
            yield test_client

    def test_up_to_date_hosts_show_no_badge(
        self,
        notice_client: TestClient,
        runner: UpdateRunner,
        machine_runner: UpdateRunner,
    ):
        runner.check()
        machine_runner.check()

        body = notice_client.get("/api/update-notice?machine_id=uitest").json()

        assert body["available"] is False
        assert body["label"] == ""

    def test_frontend_update_points_at_the_ui_update_page(
        self,
        notice_client: TestClient,
        sandbox: UpdateSandbox,
        runner: UpdateRunner,
    ):
        sandbox.push()
        runner.check()

        body = notice_client.get("/api/update-notice?machine_id=uitest").json()

        assert body["available"] is True
        assert body["href"] == "/update"
        assert body["label"]

    def test_machine_update_points_at_the_update_page(
        self,
        notice_client: TestClient,
        machine_sandbox: UpdateSandbox,
        machine_runner: UpdateRunner,
    ):
        machine_sandbox.push()
        machine_runner.check()

        body = notice_client.get("/api/update-notice?machine_id=uitest").json()

        assert body["available"] is True
        assert body["href"] == "/update"
        assert "uitest" in body["detail"]

    def test_both_hosts_pending_are_counted_together(
        self,
        notice_client: TestClient,
        sandbox: UpdateSandbox,
        runner: UpdateRunner,
        machine_sandbox: UpdateSandbox,
        machine_runner: UpdateRunner,
    ):
        sandbox.push()
        machine_sandbox.push()
        runner.check()
        machine_runner.check()

        body = notice_client.get("/api/update-notice?machine_id=uitest").json()

        assert body["available"] is True
        assert body["href"] == "/update"
        assert len(body["detail"].splitlines()) == 2

    def test_unreachable_machine_still_reports_the_frontend(
        self, ui_settings: Settings, sandbox: UpdateSandbox, runner: UpdateRunner
    ):
        """機体が落ちていてもトップバーは壊れない（誰も listen しない port へ実接続）."""
        settings = attrs.evolve(
            ui_settings,
            machines=(MachineEndpoint(machine_id="down", host="127.0.0.1", port=1),),
        )
        app = create_app(settings, update_runner=runner)
        sandbox.push()
        runner.check()

        with TestClient(app) as client:
            response = client.get("/api/update-notice?machine_id=down")

        assert response.status_code == 200
        assert response.json()["available"] is True

    def test_machineless_page_only_asks_the_frontend(
        self,
        notice_client: TestClient,
        machine_sandbox: UpdateSandbox,
        machine_runner: UpdateRunner,
    ):
        """マシン非依存のページ（機体一覧・/update）には machine_id が無い."""
        machine_sandbox.push()
        machine_runner.check()

        body = notice_client.get("/api/update-notice").json()

        assert body["available"] is False

    def test_colocated_host_is_counted_once(
        self,
        ui_settings: Settings,
        backend_settings: ApiEnv,
        paste_test_board_footprint_root: Path,
        sandbox: UpdateSandbox,
        runner: UpdateRunner,
        machine_sandbox: UpdateSandbox,
        machine_runner: UpdateRunner,
    ):
        """同居機は frontend と backend が同じホストなので 1 件にまとめる.

        畳まないと 1 つの更新が「2 件」と表示される（`PCBASM_HOSTNAME` 未設定の
        機体では両者とも `socket.gethostname()` を名乗る）。
        """
        sandbox.push()
        machine_sandbox.push()
        runner.check()
        machine_runner.check()

        with self._client(
            ui_settings,
            attrs.evolve(backend_settings, hostname=socket.gethostname()),
            paste_test_board_footprint_root,
            runner,
            machine_runner,
        ) as client:
            body = client.get("/api/update-notice?machine_id=uitest").json()

        assert body["available"] is True
        assert len(body["detail"].splitlines()) == 1

    def test_every_page_loads_the_badge(self, notice_client: TestClient):
        """トップバーは全ページ共通なので base.html から配線する."""
        body = notice_client.get("/update").text

        assert 'id="update-badge"' in body
        assert "js/update_notice.js" in body


def _panel(html: str, key: str) -> str:
    """``data-testid="update-panel-<key>"`` のパネルの HTML 断片."""
    _, _, rest = html.partition(f'data-testid="update-panel-{key}"')
    assert rest, f"{key} のパネルがありません"
    return rest.split("</section>", 1)[0]
