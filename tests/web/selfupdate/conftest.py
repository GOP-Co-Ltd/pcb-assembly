"""`src/web/selfupdate/` の契約テストが共有する実 git リポジトリ.

remote は `tmp_path` 上の bare リポジトリなので、ネットワークは使わない。
"""

from pathlib import Path

import pytest

from tests.support.selfupdate import clone_origin, init_origin


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """1 commit を持つ bare リポジトリ（remote 役）."""
    return init_origin(tmp_path)


@pytest.fixture
def clone(tmp_path: Path, origin: Path) -> Path:
    """被験体の作業リポジトリ（`main` が `origin/main` を追跡している）."""
    return clone_origin(origin, tmp_path / "clone")


@pytest.fixture
def publisher(tmp_path: Path, origin: Path) -> Path:
    """Origin に commit を積むための 2 本目の clone（被験体とは別ディレクトリ）."""
    return clone_origin(origin, tmp_path / "publisher")
