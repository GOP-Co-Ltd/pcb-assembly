from __future__ import annotations

import tomllib
from pathlib import Path

import pcb_assembly
from tests.helpers import PROJECT_ROOT


def test_version() -> None:
    with open(PROJECT_ROOT / "pyproject.toml", "rb") as f:
        pyproject = tomllib.load(f)

    assert pcb_assembly.__version__ == pyproject["project"]["version"]
