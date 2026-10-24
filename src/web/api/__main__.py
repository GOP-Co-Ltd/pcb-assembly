"""`python -m web.api` のエントリポイント."""

from __future__ import annotations

from types import FrameType
from typing import override

import uvicorn

from web.api.app import create_app
from web.api.camera import CameraFeed
from web.api.env import Env
from web.process import setup_logging


class _WebUIServer(uvicorn.Server):
    """Ctrl+C 時に開いている preview stream へ終了を通知する Server."""

    def __init__(self, config: uvicorn.Config, camera: CameraFeed) -> None:
        super().__init__(config)
        self._camera = camera

    @override
    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self._camera.request_shutdown()
        super().handle_exit(sig, frame)


def main() -> None:
    # FrameHub の start/stop ログ（E2E の停止確認に使う）を含めて出力する
    setup_logging()
    env = Env.from_env()
    app = create_app(env)
    config = uvicorn.Config(
        app,
        host=env.host,
        port=env.port,
        timeout_graceful_shutdown=3,
    )
    try:
        _WebUIServer(config, app.state.backend.camera).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
