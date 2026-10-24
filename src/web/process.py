"""Backend（web.api）と frontend（web.ui）の起動部品: env の読み取りと logging の初期化.

標準ライブラリだけに依存する（`tests/test_architecture.py`）。
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterable
from pathlib import Path


def env_path(name: str, default: Path) -> Path:
    """Env をパスとして読む（未設定か空なら既定）."""
    value = os.environ.get(name)
    return Path(value) if value else default


def env_fake() -> bool:
    """``PCBASM_FAKE=1`` のシミュレーション起動か（Klipper とカメラを fake にし、mDNS と更新を切る）."""
    return os.environ.get("PCBASM_FAKE") == "1"


def env_words(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """空白区切りの env を argv 断片として読む（未設定か空なら既定）."""
    value = os.environ.get(name)
    return tuple(value.split()) if value else default


def setup_logging(
    level: int = logging.INFO, namespaces: Iterable[str] = ("pcbasm", "web")
) -> None:
    """名前空間ごとのロガーを標準出力へ出す.

    Args:
        level: 出力するログの下限
        namespaces: 出力するロガーの名前空間
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )

    for namespace in namespaces:
        logger = logging.getLogger(namespace)
        logger.setLevel(level)
        logger.addHandler(handler)
