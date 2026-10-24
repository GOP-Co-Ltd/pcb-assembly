"""版のずれた機体（更新専用）を実サーバーで確かめる E2E.

backend を 2 台立て、片方を版の合う機体、もう片方を版のずれた機体として frontend に見せる。

- 版のずれた広告を mDNS で受けた機体は、一覧で更新専用になる
- proxy は版のずれた機体へ更新と緊急停止だけを中継し、他は 409、WS は handshake で拒む
- 一括管理の行と機体のページは更新専用になり、そこから実際に更新でき、緊急停止できる

mDNS を通すのは最初の 1 点だけで、残りは版のずれを静的登録で直接与える（mDNS の使えない環境でも走らせるため）。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import attrs
import httpx
import pytest
from playwright.sync_api import expect
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect
from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from pcbasm.hal.fake import FakeAudioPlayer
from tests.e2e.conftest import E2E_MACHINE_ID, LiveServer, RunningServer, start_app
from tests.helpers import random_service_type, skip_if_no_mdns
from tests.support.backend import devices_with_audio
from tests.support.polling import wait_until
from tests.support.settings import make_api_settings, make_ui_settings
from tests.web.api.jobs.conftest import register_gated
from tests.web.update_support import UpdateSandbox, make_update_sandbox
from web.api.app import create_app
from web.contract.discovery import API_VERSION, TXT_KEY_API, TXT_KEY_ID
from web.selfupdate.runner import UpdateRunner
from web.ui.app import create_app as create_ui_app
from web.ui.machines import MachineEndpoint

OLD_MACHINE_ID = "e2eold"
# 緊急停止で止まることを見るための、止められるまで終わらないジョブ
_STOPPABLE_JOB = "update_only_stoppable"

_HTTP_TIMEOUT = 10.0
_UI_TIMEOUT_MS = 15_000
# 探索結果を待つ上限（マルチキャストの往復 + 解決）
_DISCOVERY_TIMEOUT = 20.0

LOOPBACK = "127.0.0.1"


@attrs.frozen
class MixedStack:
    """版の合う機体と版のずれた機体を 1 台ずつ持つ frontend."""

    origin: str
    # 版のずれた機体の backend（frontend を通さずに状態を読む）
    old_backend: str

    @property
    def ws_origin(self) -> str:
        return "ws://" + self.origin.removeprefix("http://")


@pytest.fixture
def sandbox(tmp_path: Path) -> UpdateSandbox:
    """版のずれた機体の実リポジトリ."""
    return make_update_sandbox(tmp_path / "old-update")


@pytest.fixture
def old_backend(
    sandbox: UpdateSandbox, tmp_path: Path, paste_test_board_footprint_root: Path
) -> Iterator[RunningServer]:
    """版のずれた機体の backend（版のずれは広告か登録で与える）."""
    env = make_api_settings(tmp_path / "old", hostname=OLD_MACHINE_ID)
    app = create_app(
        env,
        devices=devices_with_audio(env, FakeAudioPlayer()),
        paste_test_board_footprint_root=paste_test_board_footprint_root,
        update_runner=UpdateRunner(sandbox.settings),
    )
    register_gated(app.state.backend.catalog, name=_STOPPABLE_JOB)
    old = start_app(app)
    try:
        yield old
    finally:
        old.stop()


@pytest.fixture
def mixed_stack(
    live_server: LiveServer, old_backend: RunningServer, tmp_path: Path
) -> Iterator[MixedStack]:
    endpoints = (
        MachineEndpoint(
            machine_id=E2E_MACHINE_ID,
            host=LOOPBACK,
            port=live_server.port,
        ),
        MachineEndpoint(
            machine_id=OLD_MACHINE_ID,
            host=LOOPBACK,
            port=old_backend.port,
            update_only=True,
        ),
    )
    frontend = start_app(
        create_ui_app(
            make_ui_settings(endpoints, machines_file=tmp_path / "absent-machines.toml")
        )
    )
    try:
        yield MixedStack(
            origin=f"http://{LOOPBACK}:{frontend.port}",
            old_backend=f"http://{LOOPBACK}:{old_backend.port}",
        )
    finally:
        frontend.stop()


@contextmanager
def advertised(service_type: str, *, port: int, api: str) -> Iterator[None]:
    """版のずれた機体の広告を、指定した API 版でループバックに出す.

    `ServiceAdvertiser` は自分の版しか広告しないので、ServiceInfo を手で組む。
    main thread は playwright の sync API が event loop を回しているので、専用スレッドの loop で動かす。
    """
    info = ServiceInfo(
        type_=service_type,
        name=f"{OLD_MACHINE_ID}.{service_type}",
        port=port,
        server=f"{OLD_MACHINE_ID}-pcbasm.local.",
        properties={
            TXT_KEY_ID: OLD_MACHINE_ID,
            TXT_KEY_API: api,
        },
        parsed_addresses=[LOOPBACK],
    )

    async def register() -> AsyncZeroconf:
        zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only, interfaces=[LOOPBACK])
        await zeroconf.async_register_service(info)
        return zeroconf

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="mdns-advertiser")
    thread.start()
    try:
        zeroconf = asyncio.run_coroutine_threadsafe(register(), loop).result(
            timeout=_HTTP_TIMEOUT
        )
        try:
            yield
        finally:
            asyncio.run_coroutine_threadsafe(zeroconf.async_close(), loop).result(
                timeout=_HTTP_TIMEOUT
            )
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        loop.close()


def _job_status(base_url: str) -> str | None:
    job = httpx.get(f"{base_url}/api/jobs/current", timeout=_HTTP_TIMEOUT).json()["job"]
    return None if job is None else job["status"]


def _machine_labels(origin: str) -> dict[str, str]:
    machines = httpx.get(f"{origin}/api/machines", timeout=_HTTP_TIMEOUT).json()
    return {m["machine_id"]: m["label"] for m in machines["machines"]}


class TestAdvertisedOldVersion:
    """API 版のずれた広告から、一覧の更新専用と中継の範囲まで."""

    @skip_if_no_mdns
    def test_discovered_machine_becomes_update_only(
        self, old_backend: RunningServer, tmp_path: Path
    ):
        service_type = random_service_type()
        settings = attrs.evolve(
            make_ui_settings((), machines_file=tmp_path / "absent-machines.toml"),
            discovery_enabled=True,
            discovery_service_type=service_type,
            discovery_interfaces=(LOOPBACK,),
        )
        frontend = start_app(create_ui_app(settings))
        origin = f"http://{LOOPBACK}:{frontend.port}"
        machine = f"{origin}/m/{OLD_MACHINE_ID}"
        try:
            with advertised(
                service_type, port=old_backend.port, api=str(API_VERSION + 1)
            ):
                wait_until(
                    lambda: OLD_MACHINE_ID in _machine_labels(origin),
                    timeout=_DISCOVERY_TIMEOUT,
                )
                label = _machine_labels(origin)[OLD_MACHINE_ID]
                status = httpx.get(
                    f"{machine}/api/update/status", timeout=_HTTP_TIMEOUT
                )
                state = httpx.get(f"{machine}/api/session/state", timeout=_HTTP_TIMEOUT)
        finally:
            frontend.stop()

        assert label.endswith("（更新専用）")
        assert status.status_code == 200
        assert status.json()["hostname"] == OLD_MACHINE_ID
        assert state.status_code == 409


class TestRelayScope:
    """版のずれた機体へ中継する範囲."""

    def test_update_status_is_relayed(self, mixed_stack: MixedStack):
        response = httpx.get(
            f"{mixed_stack.origin}/m/{OLD_MACHINE_ID}/api/update/status",
            timeout=_HTTP_TIMEOUT,
        )

        assert response.status_code == 200
        assert response.json()["hostname"] == OLD_MACHINE_ID

    def test_emergency_stop_is_relayed_and_aborts_the_job(
        self, mixed_stack: MixedStack
    ):
        started = httpx.post(
            f"{mixed_stack.old_backend}/api/jobs/{_STOPPABLE_JOB}",
            json={},
            timeout=_HTTP_TIMEOUT,
        )
        assert started.status_code == 201, started.text

        response = httpx.post(
            f"{mixed_stack.origin}/m/{OLD_MACHINE_ID}/api/emergency-stop",
            timeout=_HTTP_TIMEOUT,
        )

        # Moonraker 不通の 502 は backend が返したもの（proxy が断れば 409）。
        # backend はジョブを止めてから送ろうとする
        assert response.status_code == 502
        wait_until(
            lambda: _job_status(mixed_stack.old_backend) == "aborted", timeout=30.0
        )

    def test_other_paths_are_refused_only_for_the_old_machine(
        self, mixed_stack: MixedStack
    ):
        def state(machine_id: str) -> int:
            return httpx.get(
                f"{mixed_stack.origin}/m/{machine_id}/api/session/state",
                timeout=_HTTP_TIMEOUT,
            ).status_code

        assert state(OLD_MACHINE_ID) == 409
        assert state(E2E_MACHINE_ID) == 200

    def test_websocket_is_rejected_before_accept(self, mixed_stack: MixedStack):
        with pytest.raises(InvalidStatus) as rejected:
            with connect(
                f"{mixed_stack.ws_origin}/m/{OLD_MACHINE_ID}/api/jobs/ws",
                open_timeout=_HTTP_TIMEOUT,
            ):
                pass

        assert rejected.value.response.status_code == 403


class TestUpdateOnlyInTheBrowser:
    """一括管理から更新専用ページへ辿り、更新して緊急停止する."""

    def test_bulk_row_leads_to_the_update_page(
        self, mixed_stack: MixedStack, browser_page
    ):
        page = browser_page
        page.goto(f"{mixed_stack.origin}/bulk")
        expect(
            page.get_by_test_id(f"bulk-row-{E2E_MACHINE_ID}").get_by_test_id(
                "bulk-holder"
            )
        ).to_have_text("空き", timeout=_UI_TIMEOUT_MS)
        old_row = page.get_by_test_id(f"bulk-row-{OLD_MACHINE_ID}")
        expect(old_row).to_contain_text("（更新専用）")

        old_row.get_by_role("link", name="更新・緊急停止").click()

        expect(page.get_by_test_id("update-only-detail")).to_be_visible()
        expect(page.get_by_test_id("estop")).to_be_visible()

    def test_old_machine_is_updated_from_its_page(
        self, mixed_stack: MixedStack, sandbox: UpdateSandbox, browser_page
    ):
        target = sandbox.push()
        page = browser_page
        page.goto(f"{mixed_stack.origin}/m/{OLD_MACHINE_ID}/posctrl")

        page.locator("#update-check").click(timeout=_UI_TIMEOUT_MS)
        expect(page.locator("#update-summary")).to_contain_text(
            "1 件の更新", timeout=_UI_TIMEOUT_MS
        )
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("#update-run").click(timeout=_UI_TIMEOUT_MS)

        expect(page.locator("#update-state-label")).to_have_text(
            "再起動しています", timeout=_UI_TIMEOUT_MS
        )
        assert sandbox.head() == target

    def test_emergency_stop_button_reaches_the_old_machine(
        self, mixed_stack: MixedStack, browser_page
    ):
        page = browser_page
        page.goto(f"{mixed_stack.origin}/m/{OLD_MACHINE_ID}/posctrl")

        with page.expect_response(
            lambda response: response.url.endswith(
                f"/m/{OLD_MACHINE_ID}/api/emergency-stop"
            )
        ) as info:
            page.get_by_test_id("estop").click(timeout=_UI_TIMEOUT_MS)

        # Moonraker 不通の 502 は backend が返したもの（proxy が断れば 409）
        assert info.value.status == 502
