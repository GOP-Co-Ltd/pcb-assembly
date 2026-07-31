"""`python -m webui` のエントリポイント."""

from __future__ import annotations

import logging
from types import FrameType
from typing import override

import uvicorn

from pcbasm.utils import setup_logging
from webui.app import create_app
from webui.preview import PreviewService
from webui.settings import Settings


class _WebUIServer(uvicorn.Server):
    """Ctrl+C 時に開いている preview stream へ終了を通知する Server."""

    def __init__(self, config: uvicorn.Config, preview: PreviewService) -> None:
        super().__init__(config)
        self._preview = preview

    @override
    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self._preview.request_shutdown()
        super().handle_exit(sig, frame)


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
        _WebUIServer(config, app.state.preview).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
