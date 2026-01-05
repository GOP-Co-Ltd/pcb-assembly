from __future__ import annotations

import tomllib
from pathlib import Path

import pcb_assembly

PROJECT_ROOT = Path(__file__).parent.parent


def test_version() -> None:
    with open(PROJECT_ROOT / "pyproject.toml", "rb") as f:
        pyproject = tomllib.load(f)

    assert pcb_assembly.__version__ == pyproject["project"]["version"]
