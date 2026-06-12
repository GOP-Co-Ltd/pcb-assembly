"""`webui.settings.Settings` の仕様テスト.

計画書「`src/webui/settings.py`」節: 既定値と `from_env` の env 上書きが契約。
"""

from pathlib import Path

import pytest

from tests.helpers import PROJECT_ROOT
from webui.settings import Settings

ENV_VARS = (
    "PCBASM_WEBUI_CONFIGS_ROOT",
    "PCBASM_WEBUI_DATA_DIR",
    "PCBASM_WEBUI_PCB_ROOT",
    "PCBASM_WEBUI_PRINTER_CFG_LINK",
    "PCBASM_MAINSAIL_URL",
    "PCBASM_WEBUI_PORT",
    "PCBASM_WEBUI_FAKE_CAMERA",
    "PCBASM_WEBUI_FAKE_CAMERA_IMAGE",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)


class TestSettingsFromEnv:
    """Settings.from_env の振る舞い."""

    def test_defaults_without_env(self, clean_env: None):
        settings = Settings.from_env()

        assert settings.configs_root == PROJECT_ROOT / "configs"
        assert settings.data_dir == PROJECT_ROOT / "data"
        assert settings.pcb_browse_root == PROJECT_ROOT
        assert (
            settings.printer_cfg_link
            == Path.home() / "printer_data" / "config" / "printer.cfg"
        )
        # None は「ページ閲覧元ホストに追従」を意味する（pages.py で解決）
        assert settings.mainsail_url is None
        assert settings.default_machine == "kurousagi"
        assert settings.port == 8080

    def test_env_overrides_each_field(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        monkeypatch.setenv("PCBASM_WEBUI_CONFIGS_ROOT", str(tmp_path / "configs"))
        monkeypatch.setenv("PCBASM_WEBUI_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("PCBASM_WEBUI_PCB_ROOT", str(tmp_path / "pcb"))
        monkeypatch.setenv("PCBASM_WEBUI_PRINTER_CFG_LINK", str(tmp_path / "link.cfg"))
        monkeypatch.setenv("PCBASM_MAINSAIL_URL", "http://mainsail.example:8000")
        monkeypatch.setenv("PCBASM_WEBUI_PORT", "9001")

        settings = Settings.from_env()

        assert settings.configs_root == tmp_path / "configs"
        assert settings.data_dir == tmp_path / "data"
        assert settings.pcb_browse_root == tmp_path / "pcb"
        assert settings.printer_cfg_link == tmp_path / "link.cfg"
        assert settings.mainsail_url == "http://mainsail.example:8000"
        assert settings.port == 9001

    def test_non_numeric_port_raises_value_error(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("PCBASM_WEBUI_PORT", "not-a-number")

        with pytest.raises(ValueError):
            Settings.from_env()


class TestFakeCameraSettings:
    """Phase 2 追加フィールド（計画書 webui-phase2.md「src/webui/settings.py」節）."""

    def test_defaults_to_real_camera(self, clean_env: None):
        settings = Settings.from_env()

        assert settings.fake_camera is False
        assert (
            settings.fake_camera_image
            == PROJECT_ROOT / "data" / "testing" / "webui" / "fake_camera.png"
        )

    def test_env_enables_fake_camera_and_overrides_image(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        monkeypatch.setenv("PCBASM_WEBUI_FAKE_CAMERA", "1")
        monkeypatch.setenv("PCBASM_WEBUI_FAKE_CAMERA_IMAGE", str(tmp_path / "cam.png"))

        settings = Settings.from_env()

        assert settings.fake_camera is True
        assert settings.fake_camera_image == tmp_path / "cam.png"
