"""`web.ui.settings.Settings` の仕様テスト.

- 環境変数は port、fake、machines.toml を置く config の場所だけ
- **`config_dir` / `data_dir` / `pcb_*` / `fake` を一切持たない**。frontend は
  machine.toml を読まないので、機体でないホスト（`config/` が無い）でも起動できる
"""

from pathlib import Path

import attrs
import pytest

from pcbasm.paths import PROJECT_ROOT
from web.ui.settings import Settings

# backend 側の設定に由来し、frontend が持ってはいけないフィールド
FORBIDDEN_FIELDS = (
    "config_dir",
    "data_dir",
    "webui_data_dir",
    "pcb_browse_root",
    "pcb_browse_allowed",
    "pcb_browse_start",
    "pcb_upload_dir",
    "fake",
    "fake_camera_image",
)


@pytest.fixture
def real_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake でない frontend の環境（Settings を組むだけで、起動はしない）."""
    for name in ("PCBASM_FAKE", "PCBASM_UI_PORT", "PCBASM_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)


class TestFromEnv:
    def test_defaults(self, real_host: None):
        settings = Settings.from_env()

        # 同居機で backend（8081）と同じ port を掴まない
        assert settings.port == 8080
        assert settings.machines_file == PROJECT_ROOT / "config" / "machines.toml"
        # 実運用は LAN の機体を探し、frontend 自身も更新できる
        assert settings.discovery_enabled is True
        assert settings.update_enabled is True

    def test_env_overrides_port_and_config_dir(
        self, real_host: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        monkeypatch.setenv("PCBASM_UI_PORT", "9101")
        monkeypatch.setenv("PCBASM_CONFIG_DIR", str(tmp_path))

        settings = Settings.from_env()

        assert settings.port == 9101
        assert settings.machines_file == tmp_path / "machines.toml"

    def test_backend_port_does_not_leak_into_frontend(
        self, real_host: None, monkeypatch: pytest.MonkeyPatch
    ):
        """Backend の port の env で frontend の port が動かない.

        同居機では両方の env が同じシェルに並ぶ。
        """
        monkeypatch.setenv("PCBASM_API_PORT", "9999")

        assert Settings.from_env().port == 8080

    def test_fake_turns_off_discovery_and_update(self, monkeypatch: pytest.MonkeyPatch):
        """Fake の機体だけを操作させ、実 LAN の機体を探さず、更新もさせない."""
        monkeypatch.setenv("PCBASM_FAKE", "1")

        settings = Settings.from_env()

        assert settings.discovery_enabled is False
        assert settings.update_enabled is False


class TestSettingsHasNoBackendFields:
    """装置設定に触るフィールドを持たない（machine.toml 非依存の担保）."""

    @pytest.mark.parametrize("name", FORBIDDEN_FIELDS)
    def test_backend_only_field_is_absent(self, name: str):
        field_names = {field.name for field in attrs.fields(Settings)}

        assert name not in field_names
        assert not hasattr(Settings(), name)
