"""WebUI 実プロセスの graceful shutdown / restart E2E."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page
from websockets.sync.client import connect

from tests.e2e.conftest import (
    LiveServer,
    drive_job_demo,
    respond_prompt,
    wait_first_prompt,
)
from tests.helpers import PROJECT_ROOT, wait_until
from webui.settings import Settings

_HTTP_TIMEOUT = 10.0
_PROCESS_TIMEOUT = 30.0


def _free_port() -> int:
    """テストプロセス用の loopback port を OS に割り当ててもらう."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ProcessWebui:
    """`python -m webui` を同一 URL で起動・再起動するテストハーネス."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._port = _free_port()
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def live_server(self) -> LiveServer:
        return LiveServer(
            base_url=f"http://127.0.0.1:{self._port}", settings=self._settings
        )

    def start(self) -> subprocess.Popen[bytes]:
        assert self._process is None
        env = {
            **os.environ,
            "PCBASM_WEBUI_CONFIGS_ROOT": str(self._settings.configs_root),
            "PCBASM_WEBUI_DATA_DIR": str(self._settings.data_dir),
            "PCBASM_WEBUI_PCB_ROOT": str(self._settings.pcb_browse_root),
            "PCBASM_WEBUI_PORT": str(self._port),
            "PCBASM_WEBUI_FAKE_CAMERA": "1",
            "PCBASM_WEBUI_FAKE_CAMERA_IMAGE": str(self._settings.fake_camera_image),
        }
        self._process = subprocess.Popen(
            [sys.executable, "-m", "webui"],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        wait_until(self._is_ready, timeout=_PROCESS_TIMEOUT, interval=0.05)
        return self._process

    def send_signal(self, sig: signal.Signals) -> None:
        process = self._require_process()
        process.send_signal(sig)

    def wait(self) -> int:
        process = self._require_process()
        return_code = process.wait(timeout=_PROCESS_TIMEOUT)
        self._process = None
        return return_code

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                # 2 回目の signal はサーバーの緊急終了経路を通す。
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3.0)
        self._process = None

    def _is_ready(self) -> bool:
        process = self._require_process()
        if process.poll() is not None:
            pytest.fail(f"WebUI プロセスが起動中に終了しました: {process.returncode}")
        try:
            response = httpx.get(f"{self.live_server.base_url}/api/state", timeout=0.5)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    def _require_process(self) -> subprocess.Popen[bytes]:
        assert self._process is not None
        return self._process


@pytest.fixture
def process_webui(e2e_settings: Settings) -> Iterator[ProcessWebui]:
    server = ProcessWebui(e2e_settings)
    yield server
    server.close()


class TestGracefulServiceShutdown:
    """SIGTERM 後も prompt を処理し、ジョブ成功後に終了する."""

    def test_sigterm_drains_prompt_waiting_job(self, process_webui: ProcessWebui):
        process = process_webui.start()
        server = process_webui.live_server

        with connect(f"{server.ws_url}/api/ws") as ws:
            server_info = json.loads(ws.recv(timeout=10.0))
            assert server_info["type"] == "server_info"

            response = httpx.post(
                f"{server.base_url}/api/jobs/job_demo",
                json={"params": {"steps": 1, "interval": 0.0}},
                timeout=_HTTP_TIMEOUT,
            )
            assert response.status_code == 201
            prompt = wait_first_prompt(ws)

            process_webui.send_signal(signal.SIGTERM)

            rejected: httpx.Response | None = None

            def shutdown_rejects_start() -> bool:
                nonlocal rejected
                try:
                    rejected = httpx.post(
                        f"{server.base_url}/api/jobs/job_demo",
                        json={"params": {"steps": 1, "interval": 0.0}},
                        timeout=0.5,
                    )
                except httpx.HTTPError:
                    return False
                return rejected.status_code == 409

            wait_until(shutdown_rejects_start, timeout=10.0, interval=0.05)
            assert rejected is not None
            assert rejected.json()["owner"] == "webui-shutdown"
            assert process.poll() is None

            answered: set[str] = set()
            respond_prompt(ws, prompt, answered, number_answer=60.0)
            job, _ = drive_job_demo(ws, number_answer=60.0)

        assert job["status"] == "succeeded"
        assert process_webui.wait() == 0


class TestServiceRestartOverBrowser:
    """同一 URL のサーバー交代を検出し、ページと preview を復旧する."""

    def test_restart_reloads_page_and_restores_preview(
        self, process_webui: ProcessWebui, browser_page: Page
    ):
        first_process = process_webui.start()
        base_url = process_webui.live_server.base_url
        browser_page.add_init_script(
            """
            try {
              const key = "pcbasm-e2e-page-loads";
              sessionStorage.setItem(key, String(Number(sessionStorage.getItem(key) || "0") + 1));
            } catch (_) {}
            """
        )
        browser_page.goto(
            f"{base_url}/posctrl/camera_preview", wait_until="domcontentloaded"
        )
        preview = browser_page.locator('[data-testid="preview-img"]')
        preview.wait_for(state="visible", timeout=10_000)
        browser_page.wait_for_function(
            "(img) => img.complete && img.naturalWidth > 0 && img.naturalHeight > 0",
            arg=preview.element_handle(),
            timeout=10_000,
        )
        first_instance_id = browser_page.locator("body").get_attribute(
            "data-server-instance-id"
        )
        assert first_instance_id

        process_webui.send_signal(signal.SIGTERM)
        assert process_webui.wait() == 0
        assert first_process.poll() == 0
        process_webui.start()

        browser_page.wait_for_function(
            """
            (oldId) => document.body.dataset.serverInstanceId !== oldId
              && Number(sessionStorage.getItem("pcbasm-e2e-page-loads")) >= 2
            """,
            arg=first_instance_id,
            timeout=30_000,
        )
        preview = browser_page.locator('[data-testid="preview-img"]')
        preview.wait_for(state="visible", timeout=10_000)
        browser_page.wait_for_function(
            "(img) => img.complete && img.naturalWidth > 0 && img.naturalHeight > 0",
            arg=preview.element_handle(),
            timeout=10_000,
        )
