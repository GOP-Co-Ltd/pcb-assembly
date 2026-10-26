"""運用で切り替える環境変数は 5 つだけで、本番の unit は 1 つも設定しない契約テスト.

未設定なら設定と状態の置き場所はリポジトリ直下になり、自己更新の `uv sync` の引数はコード定数になる。
"""

import re

import pytest

from pcbasm.paths import PROJECT_ROOT
from web.api.env import Env
from web.selfupdate.settings import UpdateSettings

ENV_VARS = {
    "PCBASM_CONFIG_DIR",
    "PCBASM_DATA_DIR",
    "PCBASM_FAKE",
    "PCBASM_API_PORT",
    "PCBASM_UI_PORT",
}


class TestEnvVars:
    def test_src_reads_only_the_five_env_vars(self):
        names = {
            name
            for path in (PROJECT_ROOT / "src").rglob("*.py")
            for name in re.findall(r"""["'](PCBASM_\w+)["']""", path.read_text())
        }

        assert names == ENV_VARS


class TestBackendDirs:
    def test_config_and_data_default_to_the_repository(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("PCBASM_CONFIG_DIR", raising=False)
        monkeypatch.delenv("PCBASM_DATA_DIR", raising=False)

        env = Env.from_env()

        assert env.config_dir == PROJECT_ROOT / "config"
        assert env.data_dir == PROJECT_ROOT / "data"


class TestUvSyncArgs:
    def test_update_keeps_the_lock_and_the_extra_groups(self):
        """更新の `uv sync` は uv.lock を書き換えず、機体ごとに足した group を消さない.

        `--locked` が無いと作業ツリーが dirty になって以後の更新が止まる。
        """
        assert UpdateSettings().uv_sync_args == ("--locked", "--inexact")
