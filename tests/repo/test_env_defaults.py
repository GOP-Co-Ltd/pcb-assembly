"""本番の unit は env を設定しないので、backend の設定と状態の置き場所はリポジトリ直下になる契約テスト."""

import pytest

from pcbasm.paths import PROJECT_ROOT
from web.api.env import Env


class TestBackendDirs:
    def test_config_and_data_default_to_the_repository(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("PCBASM_CONFIG_DIR", raising=False)
        monkeypatch.delenv("PCBASM_API_DATA_DIR", raising=False)

        settings = Env.from_env()

        assert settings.config_dir == PROJECT_ROOT / "config"
        assert settings.data_dir == PROJECT_ROOT / "data"
