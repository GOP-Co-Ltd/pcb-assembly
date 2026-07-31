"""`python -m web.ui` のエントリポイント."""

from __future__ import annotations

import logging

import uvicorn

from pcbasm.utils import setup_logging
from web.ui.app import create_app
from web.ui.settings import Settings


def main() -> None:
    setup_logging(logging.INFO, namespaces=("pcbasm", "web"))
    settings = Settings.from_env()
    config = uvicorn.Config(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        timeout_graceful_shutdown=3,
    )
    try:
        uvicorn.Server(config).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
