"""WebUI の起動設定。env による上書きをサポートする."""

from __future__ import annotations

import os
from pathlib import Path

import attrs

from pcbasm.config import get_config_dir
from pcbasm.utils import PROJECT_ROOT


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else default


@attrs.frozen
class Settings:
    """WebUI サーバーの設定値."""

    config_dir: Path = attrs.field(factory=get_config_dir)
    data_dir: Path = PROJECT_ROOT / "data"
    # OS 全体を閲覧可能にする（USB マウント /media/... からの選択を想定）。
    # ファイルブラウザの初期表示位置は pcb_browse_start
    pcb_browse_root: Path = Path("/")
    pcb_browse_start: Path = PROJECT_ROOT
    # アップロード保存先。pcb_browse_root 配下であること（選択可能にするため）
    pcb_upload_dir: Path = PROJECT_ROOT / "uploads"
    # None の場合はページ閲覧元のホスト名に追従する（pages.py で解決）
    mainsail_url: str | None = None
    host: str = "0.0.0.0"
    port: int = 8080
    fake_camera: bool = False
    fake_camera_image: Path = (
        PROJECT_ROOT / "data" / "testing" / "webui" / "fake_camera.png"
    )

    @property
    def webui_data_dir(self) -> Path:
        """WebUI が所有する永続データのルート."""
        return self.data_dir / "webui"

    @classmethod
    def from_env(cls) -> Settings:
        """環境変数を反映した Settings を生成する.

        対応する環境変数:
            PCBASM_WEBUI_DATA_DIR, PCBASM_WEBUI_PCB_ROOT,
            PCBASM_MAINSAIL_URL, PCBASM_WEBUI_PORT,
            PCBASM_WEBUI_FAKE_CAMERA（"1" で固定画像カメラを使用）,
            PCBASM_WEBUI_FAKE_CAMERA_IMAGE

        ``config_dir`` は既定値の生成時点で ``PCBASM_CONFIG_DIR`` を解決するため
        （``pcbasm.config.get_config_dir``）ここでは扱わない。

        Raises:
            ValueError: PCBASM_WEBUI_PORT が整数として解釈できない場合
        """
        base = cls()
        port_env = os.environ.get("PCBASM_WEBUI_PORT")
        return cls(
            data_dir=_env_path("PCBASM_WEBUI_DATA_DIR", base.data_dir),
            pcb_browse_root=_env_path("PCBASM_WEBUI_PCB_ROOT", base.pcb_browse_root),
            mainsail_url=os.environ.get("PCBASM_MAINSAIL_URL", base.mainsail_url),
            port=int(port_env) if port_env else base.port,
            fake_camera=os.environ.get("PCBASM_WEBUI_FAKE_CAMERA") == "1",
            fake_camera_image=_env_path(
                "PCBASM_WEBUI_FAKE_CAMERA_IMAGE", base.fake_camera_image
            ),
        )
