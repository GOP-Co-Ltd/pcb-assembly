"""`web.ui.settings.Settings` の仕様テスト.

計画書 docs/plans/web-api-ui-split.md「MR4」節の契約:

- 既定値（`port=8080` / `ssr_timeout=2.0` / `backend_connect_timeout=2.0` /
  `proxy_read_timeout=120.0` / `default_backend_port=8081`）と `PCBASM_UI_*` の上書き
- **`config_dir` / `data_dir` / `pcb_*` / `fake_camera` を一切持たない**。frontend は
  machine.toml を読まないので、機体でないホスト（`config/` が無い）でも起動できる。
  フィールドを 1 つでも足すと `get_config_dir()` 依存が復活し、この要件が壊れる
"""

from pathlib import Path

import attrs
import pytest

from pcbasm.utils import PROJECT_ROOT
from web.ui.settings import Settings

# PCBASM_UI_DISCOVERY_ENABLED はここに足さない。tests/conftest.py の autouse fixture
# が全テストで "0" を入れており、delenv すると実 LAN への mDNS 探索が復活する
# （env を外して既定値を確かめるのは TestDiscoveryKillSwitch の 1 テストだけ）
ENV_VARS = (
    "PCBASM_UI_HOST",
    "PCBASM_UI_PORT",
    "PCBASM_UI_MACHINES_FILE",
    "PCBASM_UI_SSR_TIMEOUT",
    "PCBASM_UI_BACKEND_CONNECT_TIMEOUT",
    "PCBASM_UI_PROXY_READ_TIMEOUT",
    "PCBASM_UI_DEFAULT_BACKEND_PORT",
)

# backend 側の設定に由来し、frontend が持ってはいけないフィールド
FORBIDDEN_FIELDS = (
    "config_dir",
    "data_dir",
    "webui_data_dir",
    "pcb_browse_root",
    "pcb_browse_allowed",
    "pcb_browse_start",
    "pcb_upload_dir",
    "fake_camera",
    "fake_camera_image",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)


class TestSettingsDefaults:
    """既定値（env 無し）."""

    def test_defaults_without_env(self, clean_env: None):
        settings = Settings.from_env()

        assert settings.host == "0.0.0.0"
        # 同居機で backend（8081）と衝突しないこと
        assert settings.port == 8080
        assert settings.machines == ()
        assert settings.machines_file == PROJECT_ROOT / "config" / "machines.toml"
        assert settings.ssr_timeout == 2.0
        assert settings.backend_connect_timeout == 2.0
        assert settings.proxy_read_timeout == 120.0
        assert settings.default_backend_port == 8081

    def test_constructing_without_arguments_needs_no_machine_config(self):
        """引数なしで組めること（config/ が無いホストでも起動できる前提）."""
        assert Settings().machines == ()


class TestSettingsHasNoBackendFields:
    """装置設定に触るフィールドを持たない（machine.toml 非依存の担保）."""

    @pytest.mark.parametrize("name", FORBIDDEN_FIELDS)
    def test_backend_only_field_is_absent(self, name: str):
        field_names = {field.name for field in attrs.fields(Settings)}

        assert name not in field_names
        assert not hasattr(Settings(), name)


class TestSettingsFromEnv:
    """`PCBASM_UI_*` による上書き."""

    def test_env_overrides_each_field(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        monkeypatch.setenv("PCBASM_UI_HOST", "127.0.0.1")
        monkeypatch.setenv("PCBASM_UI_PORT", "9101")
        monkeypatch.setenv("PCBASM_UI_MACHINES_FILE", str(tmp_path / "machines.toml"))
        monkeypatch.setenv("PCBASM_UI_SSR_TIMEOUT", "0.5")
        monkeypatch.setenv("PCBASM_UI_BACKEND_CONNECT_TIMEOUT", "0.25")
        monkeypatch.setenv("PCBASM_UI_PROXY_READ_TIMEOUT", "30")
        monkeypatch.setenv("PCBASM_UI_DEFAULT_BACKEND_PORT", "18081")

        settings = Settings.from_env()

        assert settings.host == "127.0.0.1"
        assert settings.port == 9101
        assert settings.machines_file == tmp_path / "machines.toml"
        assert settings.ssr_timeout == 0.5
        assert settings.backend_connect_timeout == 0.25
        assert settings.proxy_read_timeout == 30.0
        assert settings.default_backend_port == 18081

    def test_backend_env_does_not_leak_into_frontend(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ):
        """Backend 用の env（`PCBASM_API_*`）で frontend の port が動かない.

        同居機では両方の env が同じシェルに並ぶため、prefix を取り違えると frontend が backend の
        port を奪う。
        """
        monkeypatch.setenv("PCBASM_API_PORT", "9999")

        assert Settings.from_env().port == 8080

    @pytest.mark.parametrize(
        "name", ("PCBASM_UI_PORT", "PCBASM_UI_DEFAULT_BACKEND_PORT")
    )
    def test_non_numeric_port_raises_value_error(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch, name: str
    ):
        monkeypatch.setenv(name, "not-a-number")

        with pytest.raises(ValueError):
            Settings.from_env()

    @pytest.mark.parametrize(
        "name",
        (
            "PCBASM_UI_SSR_TIMEOUT",
            "PCBASM_UI_BACKEND_CONNECT_TIMEOUT",
            "PCBASM_UI_PROXY_READ_TIMEOUT",
        ),
    )
    def test_non_numeric_timeout_raises_value_error(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch, name: str
    ):
        monkeypatch.setenv(name, "soon")

        with pytest.raises(ValueError):
            Settings.from_env()


class TestDiscoveryKillSwitch:
    """`PCBASM_UI_DISCOVERY_ENABLED` — 実 LAN の mDNS 探索を止めるスイッチ.

    `make ui-fake` とテスト用の autouse fixture がこの env だけで探索を止めるので、 判定（`!=
    "0"`）が壊れると隔離が丸ごと崩れる（fake backend だけを見るはずの frontend
    に実機が混ざる）。既定は「探索する」。
    """

    def test_unset_env_discovers(self, monkeypatch: pytest.MonkeyPatch):
        """既定は有効。だから ui-fake と fixture 側の明示的な "0" が要件になる."""
        monkeypatch.delenv("PCBASM_UI_DISCOVERY_ENABLED", raising=False)

        assert Settings.from_env().discovery_enabled is True

    def test_zero_disables_discovery(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("PCBASM_UI_DISCOVERY_ENABLED", "0")

        assert Settings.from_env().discovery_enabled is False

    def test_other_values_keep_discovery(self, monkeypatch: pytest.MonkeyPatch):
        """明示的な "0" 以外は有効（誤設定で黙って探索が止まらない）."""
        monkeypatch.setenv("PCBASM_UI_DISCOVERY_ENABLED", "1")

        assert Settings.from_env().discovery_enabled is True
