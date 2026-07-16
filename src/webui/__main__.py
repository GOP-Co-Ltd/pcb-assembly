"""`python -m webui` のエントリポイント."""

from __future__ import annotations

import logging
import threading
from types import FrameType
from typing import override

import uvicorn

from pcbasm.utils import setup_logging
from webui.app import create_app
from webui.jobs.manager import JobManager
from webui.preview import PreviewService
from webui.settings import Settings


class _WebUIServer(uvicorn.Server):
    """実行中ジョブを自然終了させてから WebUI を停止する Server."""

    def __init__(
        self, config: uvicorn.Config, preview: PreviewService, jobs: JobManager
    ) -> None:
        super().__init__(config)
        self._preview = preview
        self._jobs = jobs
        self._shutdown_waiter: threading.Thread | None = None

    @override
    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        if self._shutdown_waiter is None:
            self._jobs.begin_shutdown()
            self._shutdown_waiter = threading.Thread(
                target=self._wait_for_idle,
                name="webui-shutdown-waiter",
                daemon=True,
            )
            self._shutdown_waiter.start()
            return
        # 追加シグナルは Uvicorn 標準の緊急停止経路へ渡す。
        super().handle_exit(sig, frame)

    def _wait_for_idle(self) -> None:
        self._jobs.wait_for_idle()
        self._preview.request_shutdown()
        self.should_exit = True


def main() -> None:
    # FrameHub の start/stop ログ（E2E の停止確認に使う）を含めて出力する
    setup_logging(logging.INFO, namespaces=("pcbasm", "webui"))
    settings = Settings.from_env()
    app = create_app(settings)
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        timeout_graceful_shutdown=3,
    )
    try:
        _WebUIServer(config, app.state.preview, app.state.jobs).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
