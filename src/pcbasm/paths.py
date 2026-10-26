"""リポジトリ直下の場所と、config / data の置き場所の解決."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent


def _env_dir(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else default


def config_dir() -> Path:
    """マシン設定の置き場所（``PCBASM_CONFIG_DIR``。未設定ならリポジトリ直下の config/）."""
    return _env_dir("PCBASM_CONFIG_DIR", PROJECT_ROOT / "config")


def data_dir() -> Path:
    """成果物と状態の置き場所（``PCBASM_DATA_DIR``。未設定ならリポジトリ直下の data/）."""
    return _env_dir("PCBASM_DATA_DIR", PROJECT_ROOT / "data")
