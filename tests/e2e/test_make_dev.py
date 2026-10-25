"""`make dev` を実際に起動し、fake の機体ではんだ塗布を最後まで通す.

- 設定の変更は一時ディレクトリに閉じ、終了すると一時ディレクトリごと消える
- どちらかのサーバーが終わると、もう片方も止めて終了コード 1 で終わる
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from pcbasm.paths import PROJECT_ROOT
from tests.e2e.conftest import TERMINAL
from tests.support.polling import wait_until
from tests.web.api.conftest import COPPER_PCB_FIXTURE
from web.dev import SIM_CONFIG


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _env(api_port: int, ui_port: int) -> dict[str, str]:
    return {
        key: value for key, value in os.environ.items() if not key.startswith("PCBASM_")
    } | {"PCBASM_API_PORT": str(api_port), "PCBASM_UI_PORT": str(ui_port)}


def _sim_config() -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in SIM_CONFIG.iterdir()}


@contextmanager
def _make_dev(
    log_path: Path, tmpdir: Path
) -> Iterator[tuple[httpx.Client, httpx.Client]]:
    """`make dev` を起動し、(backend, frontend) のクライアントを返す（Ctrl-C と同じ経路で止める）.

    Args:
        log_path: `make dev` の出力の書き先
        tmpdir: `make dev` が一時ディレクトリを作る場所（TMPDIR）
    """
    api_port, ui_port = _free_port(), _free_port()
    env = _env(api_port, ui_port) | {"TMPDIR": str(tmpdir)}
    with (
        log_path.open("w") as log,
        httpx.Client(base_url=f"http://127.0.0.1:{api_port}", timeout=5.0) as api,
        httpx.Client(base_url=f"http://127.0.0.1:{ui_port}", timeout=5.0) as ui,
    ):
        process = subprocess.Popen(
            ["make", "dev"],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:

            def ready() -> bool:
                assert process.poll() is None, log_path.read_text()
                try:
                    return (
                        api.get("/api/session/state").status_code == 200
                        and ui.get("/api/machines").status_code == 200
                    )
                except httpx.TransportError:
                    return False

            wait_until(ready, timeout=30.0, interval=0.1)
            yield api, ui
        finally:
            os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=15.0)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5.0)
                pytest.fail(f"make dev が終了しませんでした: {log_path.read_text()}")


class TestMakeDev:
    def test_paste_solder_runs_on_the_fake_machine(self, tmp_path: Path):
        with _make_dev(tmp_path / "make-dev.log", tmp_path) as (api, ui):
            machines = ui.get("/api/machines").json()["machines"]
            assert [machine["machine_id"] for machine in machines] == ["fake"]
            assert ui.get("/api/self-update").json()["enabled"] is False
            assert api.get("/api/update/status").json()["enabled"] is False

            assert api.post("/api/session/control/acquire").status_code == 200
            selected = api.put(
                "/api/selection/pcb-file",
                json={"path": str(COPPER_PCB_FIXTURE.relative_to("/"))},
            )
            assert selected.status_code == 200, selected.text
            started = api.post("/api/jobs/paste_solder", json={"params": {}})
            assert started.status_code == 201, started.text
            job_id = started.json()["job"]["id"]

            def finished() -> bool:
                job = api.get("/api/jobs/current").json()["job"]
                return job["id"] == job_id and job["status"] in TERMINAL

            wait_until(finished, timeout=120.0, interval=0.2)
            job = api.get("/api/jobs/current").json()["job"]

        assert job["status"] == "succeeded", job

    def test_settings_stay_in_a_temp_dir_removed_on_exit(self, tmp_path: Path):
        before = _sim_config()
        with _make_dev(tmp_path / "make-dev.log", tmp_path) as (api, _):
            assert len(list(tmp_path.glob("pcbasm-dev-*"))) == 1
            assert api.post("/api/session/control/acquire").status_code == 200
            saved = api.put(
                "/api/machine_config", json={"values": {"audio.volume": 0.4}}
            )
            assert saved.status_code == 200, saved.text

        assert _sim_config() == before
        assert list(tmp_path.glob("pcbasm-dev-*")) == []

    def test_stops_the_other_server_and_fails_when_one_exits(self, tmp_path: Path):
        """Backend のポートが塞がっていて起動できなければ、frontend も止めて終わる."""
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            env = _env(taken.getsockname()[1], _free_port()) | {"TMPDIR": str(tmp_path)}
            result = subprocess.run(
                [sys.executable, "-m", "web.dev"],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=60.0,
            )

        assert result.returncode == 1, result.stdout + result.stderr
        assert list(tmp_path.glob("pcbasm-dev-*")) == []
