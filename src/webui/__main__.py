"""`python -m webui` のエントリポイント."""

from __future__ import annotations

import logging

import uvicorn

from pcbasm.utils import setup_logging
from webui.settings import Settings


def main() -> None:
    # FrameHub の start/stop ログ（E2E の停止確認に使う）を含めて出力する
    setup_logging(logging.INFO, namespaces=("pcbasm", "webui"))
    settings = Settings.from_env()
    uvicorn.run(
        "webui.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
    )


if __name__ == "__main__":
    main()
