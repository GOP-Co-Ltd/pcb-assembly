"""`web.api.settings.Settings` の仕様テスト.

計画書「`src/webui/settings.py`」節: 既定値と `from_env` の env 上書きが契約。

MR2（計画書 docs/plans/web-api-ui-split.md「MR2」節）が追記契約:

- `pcb_browse_allowed` — 実際に公開するサブツリー。既定はリポジトリルート +
  `/media` + `/mnt`（`pcb_browse_root` は board_id の基準なので `/` から動かさない）
- `hostname` — backend の自己申告 ID の注入口。既定 None（`socket.gethostname()`）
- `PCBASM_API_PCB_ROOT` を与えたときは、その root も `pcb_browse_allowed` に入る
  （入れないとファイルブラウザが全パス 400 になる）
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.helpers import PROJECT_ROOT
from web.api.app import create_app
from web.api.settings import Settings

ENV_VARS = (
    "PCBASM_CONFIG_DIR",
    "PCBASM_API_DATA_DIR",
    "PCBASM_API_PCB_ROOT",
    "PCBASM_MAINSAIL_URL",
    "PCBASM_API_PORT",
    "PCBASM_API_FAKE_CAMERA",
    "PCBASM_API_FAKE_CAMERA_IMAGE",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)


class TestSettingsFromEnv:
    """Settings.from_env の振る舞い."""

    def test_defaults_without_env(self, clean_env: None):
        settings = Settings.from_env()

        assert settings.config_dir == PROJECT_ROOT / "config"
        assert settings.data_dir == PROJECT_ROOT / "data"
        assert settings.webui_data_dir == PROJECT_ROOT / "data" / "webui"
        # root は board_id の算出基準なので "/" 固定。公開範囲は allowed で絞る
        assert settings.pcb_browse_root == Path("/")
        assert settings.pcb_browse_allowed == (
            PROJECT_ROOT,
            Path("/media"),
            Path("/mnt"),
        )
        assert settings.pcb_browse_start == PROJECT_ROOT
        assert settings.pcb_upload_dir == PROJECT_ROOT / "uploads"
        # None は「machine_id（= hostname）から解決」を意味する（routers/common.py）
        assert settings.mainsail_url is None
        assert settings.hostname is None
        # 8080 は UI frontend の既定。backend は 8081（同居機で共存させる）
        assert settings.port == 8081

    def test_env_overrides_each_field(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        monkeypatch.setenv("PCBASM_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("PCBASM_API_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("PCBASM_API_PCB_ROOT", str(tmp_path / "pcb"))
        monkeypatch.setenv("PCBASM_MAINSAIL_URL", "http://mainsail.example:8000")
        monkeypatch.setenv("PCBASM_API_PORT", "9001")

        settings = Settings.from_env()

        assert settings.config_dir == tmp_path / "config"
        assert settings.data_dir == tmp_path / "data"
        assert settings.webui_data_dir == tmp_path / "data" / "webui"
        assert settings.pcb_browse_root == tmp_path / "pcb"
        assert settings.mainsail_url == "http://mainsail.example:8000"
        assert settings.port == 9001

    def test_pcb_root_env_is_added_to_allowed_subtrees(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """Root を env で差し替えたら公開範囲もそこへ追従する（既定 3 パスは残す）."""
        monkeypatch.setenv("PCBASM_API_PCB_ROOT", str(tmp_path / "pcb"))

        settings = Settings.from_env()

        assert settings.pcb_browse_allowed == (
            PROJECT_ROOT,
            Path("/media"),
            Path("/mnt"),
            tmp_path / "pcb",
        )

    def test_non_numeric_port_raises_value_error(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("PCBASM_API_PORT", "not-a-number")

        with pytest.raises(ValueError):
            Settings.from_env()


class TestPcbRootEnvIsBrowsable:
    """`PCBASM_API_PCB_ROOT` で差し替えた root が実アプリで閲覧・選択できる.

    `Settings` を組むだけの assert では、`pcb_browse_allowed` が root に追従して
    いなくても通ってしまう（実測: 追従前は `GET /api/files` と `PUT /api/pcb-file` が
    全パス 400 でファイルブラウザが使えなかった）。公開範囲の判定は router 側に
    あるので、実 HTTP 経路で確認する。
    """

    @pytest.fixture
    def env_client(
        self,
        clean_env: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        config_dir: Path,
        pcb_root: Path,
    ) -> Iterator[TestClient]:
        """Env だけで組み立てた Settings（`from_env`）で起動した TestClient."""
        monkeypatch.setenv("PCBASM_CONFIG_DIR", str(config_dir))
        monkeypatch.setenv("PCBASM_API_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("PCBASM_API_PCB_ROOT", str(pcb_root))
        with TestClient(create_app(Settings.from_env())) as client:
            yield client

    @pytest.mark.parametrize("path", ("", "boards"))
    def test_files_listing_under_the_env_root_returns_200(
        self, env_client: TestClient, path: str
    ):
        response = env_client.get("/api/files", params={"path": path})

        assert response.status_code == 200

    def test_pcb_file_under_the_env_root_can_be_selected(self, env_client: TestClient):
        response = env_client.put("/api/pcb-file", json={"path": "top.kicad_pcb"})

        assert response.status_code == 200
        assert response.json()["pcb_file"] == "top.kicad_pcb"


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
        monkeypatch.setenv("PCBASM_API_FAKE_CAMERA", "1")
        monkeypatch.setenv("PCBASM_API_FAKE_CAMERA_IMAGE", str(tmp_path / "cam.png"))

        settings = Settings.from_env()

        assert settings.fake_camera is True
        assert settings.fake_camera_image == tmp_path / "cam.png"
