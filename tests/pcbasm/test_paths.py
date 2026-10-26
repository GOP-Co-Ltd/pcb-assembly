"""`pcbasm.paths` の仕様テスト.

config と data の置き場所は環境変数で差し替えられ、未設定ならリポジトリ直下になる（本番の unit は環境変数を設定しない）。
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from pcbasm import paths
from pcbasm.paths import PROJECT_ROOT


class TestDirs:
    @pytest.mark.parametrize(
        ("resolve", "name", "default"),
        [
            (paths.config_dir, "PCBASM_CONFIG_DIR", PROJECT_ROOT / "config"),
            (paths.data_dir, "PCBASM_DATA_DIR", PROJECT_ROOT / "data"),
        ],
    )
    def test_defaults_to_the_repository(
        self,
        monkeypatch: pytest.MonkeyPatch,
        resolve: Callable[[], Path],
        name: str,
        default: Path,
    ):
        monkeypatch.delenv(name, raising=False)

        assert resolve() == default

    @pytest.mark.parametrize(
        ("resolve", "name"),
        [
            (paths.config_dir, "PCBASM_CONFIG_DIR"),
            (paths.data_dir, "PCBASM_DATA_DIR"),
        ],
    )
    def test_env_overrides(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        resolve: Callable[[], Path],
        name: str,
    ):
        monkeypatch.setenv(name, str(tmp_path))

        assert resolve() == tmp_path
