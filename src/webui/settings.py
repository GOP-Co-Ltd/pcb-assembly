"""WebUI の起動設定。env による上書きをサポートする."""

from __future__ import annotations

import os
from pathlib import Path

import attrs

from pcbasm.utils import PROJECT_ROOT


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else default


@attrs.frozen
class Settings:
    """WebUI サーバーの設定値."""

    configs_root: Path = PROJECT_ROOT / "configs"
    data_dir: Path = PROJECT_ROOT / "data"
    pcb_browse_root: Path = PROJECT_ROOT
    printer_cfg_link: Path = Path.home() / "printer_data/config/printer.cfg"
    mainsail_url: str = "http://localhost"
    default_machine: str = "kurousagi"
    host: str = "0.0.0.0"
    port: int = 8080
    fake_camera: bool = False
    fake_camera_image: Path = (
        PROJECT_ROOT / "data" / "testing" / "webui" / "fake_camera.png"
    )

    @classmethod
    def from_env(cls) -> Settings:
        """環境変数を反映した Settings を生成する.

        対応する環境変数:
            PCBASM_WEBUI_CONFIGS_ROOT, PCBASM_WEBUI_DATA_DIR,
            PCBASM_WEBUI_PCB_ROOT, PCBASM_WEBUI_PRINTER_CFG_LINK,
            PCBASM_MAINSAIL_URL, PCBASM_WEBUI_PORT,
            PCBASM_WEBUI_FAKE_CAMERA（"1" で固定画像カメラを使用）,
            PCBASM_WEBUI_FAKE_CAMERA_IMAGE

        Raises:
            ValueError: PCBASM_WEBUI_PORT が整数として解釈できない場合
        """
        base = cls()
        port_env = os.environ.get("PCBASM_WEBUI_PORT")
        return cls(
            configs_root=_env_path("PCBASM_WEBUI_CONFIGS_ROOT", base.configs_root),
            data_dir=_env_path("PCBASM_WEBUI_DATA_DIR", base.data_dir),
            pcb_browse_root=_env_path("PCBASM_WEBUI_PCB_ROOT", base.pcb_browse_root),
            printer_cfg_link=_env_path(
                "PCBASM_WEBUI_PRINTER_CFG_LINK", base.printer_cfg_link
            ),
            mainsail_url=os.environ.get("PCBASM_MAINSAIL_URL", base.mainsail_url),
            port=int(port_env) if port_env else base.port,
            fake_camera=os.environ.get("PCBASM_WEBUI_FAKE_CAMERA") == "1",
            fake_camera_image=_env_path(
                "PCBASM_WEBUI_FAKE_CAMERA_IMAGE", base.fake_camera_image
            ),
        )
