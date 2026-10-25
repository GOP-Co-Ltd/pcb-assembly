"""`web.api.env.Env` の仕様テスト.

環境変数は config / data の置き場所、port、fake だけで、ほかはコード定数か fake にまとまる。 config と
data の既定（リポジトリ直下）は tests/repo/test_env_defaults.py が固定する。
"""

from pathlib import Path

import pytest

from pcbasm.paths import PROJECT_ROOT
from web.api.env import Env


@pytest.fixture
def real_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake でない機体の環境（Env を組むだけで、起動はしない）."""
    for name in ("PCBASM_FAKE", "PCBASM_API_PORT"):
        monkeypatch.delenv(name, raising=False)


class TestFromEnv:
    def test_defaults(self, real_machine: None):
        env = Env.from_env()

        # root は board_id の算出基準なので "/" 固定。公開範囲は allowed で絞る
        assert env.pcb_browse_root == Path("/")
        assert env.pcb_browse_allowed == (PROJECT_ROOT, Path("/media"), Path("/mnt"))
        assert env.pcb_browse_start == PROJECT_ROOT
        assert env.pcb_upload_dir == PROJECT_ROOT / "uploads"
        assert env.hostname is None
        # 8080 は UI frontend の既定。backend は 8081（同居機で共存させる）
        assert env.port == 8081
        assert env.fake is False
        assert env.fake_camera_image is None
        # 実運用は frontend から自動発見され、WebUI から更新できる
        assert env.discovery_enabled is True
        assert env.update_enabled is True

    def test_env_overrides_dirs_and_port(
        self, real_machine: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        monkeypatch.setenv("PCBASM_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("PCBASM_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("PCBASM_API_PORT", "9001")

        env = Env.from_env()

        assert env.config_dir == tmp_path / "config"
        assert env.data_dir == tmp_path / "data"
        assert env.webui_data_dir == tmp_path / "data" / "webui"
        assert env.paste_dataset_dir == tmp_path / "data" / "paste-volume-datasets"
        assert env.port == 9001

    def test_non_numeric_port_raises_value_error(
        self, real_machine: None, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("PCBASM_API_PORT", "not-a-number")

        with pytest.raises(ValueError):
            Env.from_env()


class TestFake:
    """`PCBASM_FAKE=1` — 実 LAN に広告せず、更新もさせない.

    fake の機体は実機と同じ machine_id を名乗り、実機と同じリポジトリで動きうる。
    """

    def test_fake_turns_off_discovery_and_update(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("PCBASM_FAKE", "1")

        env = Env.from_env()

        assert env.fake is True
        assert env.discovery_enabled is False
        assert env.update_enabled is False

    def test_fake_keeps_uploads_in_the_data_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """実機と同じリポジトリで動かしても、アップロードが実機の uploads/ の基板を上書きしない."""
        monkeypatch.setenv("PCBASM_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("PCBASM_FAKE", "1")

        env = Env.from_env()

        assert env.pcb_upload_dir == tmp_path / "data" / "uploads"
        assert env.pcb_upload_dir in env.pcb_browse_allowed
