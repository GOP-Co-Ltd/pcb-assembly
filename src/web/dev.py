"""`make dev`: fake の backend と frontend を、隔離ポートと一時ディレクトリで同時に起動する.

Klipper もカメラも fake にする（``PCBASM_FAKE=1``）。
設定は data/sim/config を一時ディレクトリへ複製して使い、終了すると一時ディレクトリごと消える。
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from pcbasm.paths import PROJECT_ROOT

SIM_CONFIG = PROJECT_ROOT / "data" / "sim" / "config"
API_PORT = "8099"
UI_PORT = "8098"
# frontend に静的登録する fake の機体
MACHINE_ID = "fake"


def main() -> int:
    """Backend と frontend を起動し、Ctrl-C かどちらかが終わるまで待つ.

    Returns:
        終了コード（Ctrl-C なら 0、どちらかが先に終わったら 1）
    """
    api_port = os.environ.get("PCBASM_API_PORT") or API_PORT
    ui_port = os.environ.get("PCBASM_UI_PORT") or UI_PORT
    with tempfile.TemporaryDirectory(prefix="pcbasm-dev-") as tmp:
        root = Path(tmp)
        config_dir = root / "config"
        shutil.copytree(SIM_CONFIG, config_dir)
        machines = root / "machines.toml"
        machines.write_text(
            f'[[machine]]\nmachine_id = "{MACHINE_ID}"\n'
            f'host = "127.0.0.1"\nport = {api_port}\n',
            encoding="utf-8",
        )
        env = os.environ | {"PCBASM_FAKE": "1"}
        # 子は別のプロセスグループに置き、端末の Ctrl-C は dev.py だけが受けて 1 回だけ転送する
        servers = [
            subprocess.Popen(
                [sys.executable, "-m", "web.api"],
                process_group=0,
                env=env
                | {
                    "PCBASM_CONFIG_DIR": str(config_dir),
                    "PCBASM_API_DATA_DIR": str(root / "data"),
                    "PCBASM_API_UPDATE_STATE_DIR": str(root / "selfupdate"),
                    "PCBASM_API_PORT": api_port,
                },
            ),
            subprocess.Popen(
                [sys.executable, "-m", "web.ui"],
                process_group=0,
                env=env
                | {
                    "PCBASM_UI_MACHINES_FILE": str(machines),
                    "PCBASM_UI_UPDATE_STATE_DIR": str(root / "selfupdate"),
                    "PCBASM_UI_PORT": ui_port,
                },
            ),
        ]
        print(f"http://localhost:{ui_port}/ を開く（Ctrl-C で終了）", flush=True)
        try:
            while all(server.poll() is None for server in servers):
                time.sleep(0.5)
            code = 1
        except KeyboardInterrupt:
            code = 0
        # 子の終了を待つ間に重ねて届く Ctrl-C は捨てる（待たずに一時ディレクトリを消さない）
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        for server in servers:
            if server.poll() is None:
                server.send_signal(signal.SIGINT)
        for server in servers:
            server.wait()
        return code


if __name__ == "__main__":
    sys.exit(main())
