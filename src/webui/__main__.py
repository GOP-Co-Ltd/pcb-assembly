"""`python -m webui` のエントリポイント."""

from __future__ import annotations

import uvicorn

from webui.settings import Settings


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        "webui.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
    )


if __name__ == "__main__":
    main()
