"""実際の make ターゲットで起動し、設定の隔離・再起動後の保持を確認する。"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from pcbasm.utils import PROJECT_ROOT
from tests.helpers import TESTING_CONFIG_DIR, copy_testing_config, wait_until


@contextmanager
def _launch(target: str, root: Path, env: dict[str, str]) -> Iterator[httpx.Client]:
    """子プロセスの寿命をテスト内に閉じ、Ctrl-C と同じ終了経路を通す。"""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    prefix = "PCBASM_API" if target == "api-fake" else "PCBASM_UI"
    process_env = (
        {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("PCBASM_")
        }
        | env
        | {f"{prefix}_PORT": str(port)}
    )
    log_path = root / f"{target}.log"
    with (
        log_path.open("w") as log,
        httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2.0) as client,
    ):
        process = subprocess.Popen(
            ["make", target],
            cwd=PROJECT_ROOT,
            env=process_env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:

            def ready() -> bool:
                assert process.poll() is None, log_path.read_text()
                try:
                    path = "/api/state" if target == "api-fake" else "/api/machines"
                    return client.get(path).status_code == 200
                except httpx.TransportError:
                    return False

            wait_until(ready, timeout=20.0, interval=0.1)
            yield client
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=10.0)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5.0)
                    pytest.fail(
                        f"{target} が終了しませんでした: {log_path.read_text()}"
                    )


def _field(client: httpx.Client, key: str) -> object:
    response = client.get("/api/settings/machine")
    assert response.status_code == 200, response.text
    return next(
        field["value"] for field in response.json()["fields"] if field["key"] == key
    )


class TestFakeLaunchers:
    @pytest.mark.parametrize(
        "explicit_config", [False, True], ids=["default", "explicit"]
    )
    def test_api_uses_editable_test_config_and_keeps_changes_after_restart(
        self, tmp_path: Path, explicit_config: bool
    ):
        data_dir = tmp_path / "api data"
        env = {"PCBASM_API_DATA_DIR": str(data_dir)}
        config_dir = data_dir / "config"
        if explicit_config:
            config_dir = copy_testing_config(tmp_path / "custom config")
            env["PCBASM_CONFIG_DIR"] = str(config_dir)
        source = (TESTING_CONFIG_DIR / "machine.toml").read_bytes()

        with _launch("api-fake", tmp_path, env) as client:
            assert _field(client, "camera.crop.width") == 600
            assert client.get("/api/update/status").json()["enabled"] is False
            assert client.post("/api/control/acquire").status_code == 200
            changed = client.put(
                "/api/settings/machine", json={"values": {"camera.crop.width": 444}}
            )
            assert changed.status_code == 200, changed.text

        saved = tomllib.loads((config_dir / "machine.toml").read_text())
        assert saved["klipper"]["port"] == 7126
        assert saved["camera"]["crop"]["width"] == 444
        assert (TESTING_CONFIG_DIR / "machine.toml").read_bytes() == source
        with _launch("api-fake", tmp_path, env) as client:
            assert _field(client, "camera.crop.width") == 444

    def test_ui_accepts_a_directory_with_spaces_and_disables_updates(
        self, tmp_path: Path
    ):
        fake_dir = tmp_path / "ui data"
        with _launch(
            "ui-fake", tmp_path, {"PCBASM_UI_FAKE_DIR": str(fake_dir)}
        ) as client:
            machines = client.get("/api/machines").json()["machines"]
            assert [machine["machine_id"] for machine in machines] == ["fake"]
            assert client.get("/api/self-update").json()["enabled"] is False
        config = tomllib.loads((fake_dir / "machines.toml").read_text())
        assert config["machine"][0]["port"] == 8099
