"""Backend WebAPI の起動設定。env による上書きをサポートする."""

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
    """Backend WebAPI サーバーの設定値."""

    config_dir: Path = attrs.field(factory=get_config_dir)
    data_dir: Path = PROJECT_ROOT / "data"
    # 閲覧範囲は pcb_browse_allowed で絞る。root は board_id の算出基準
    # （board_settings）なので "/" から動かさない。
    # ファイルブラウザの初期表示位置は pcb_browse_start
    pcb_browse_root: Path = Path("/")
    # 実際に閲覧・選択を許可するサブツリー（USB マウント /media/... からの選択を想定）。
    # セキュリティ境界なので他フィールドから暗黙に導出しない（明示指定のみ）
    pcb_browse_allowed: tuple[Path, ...] = (PROJECT_ROOT, Path("/media"), Path("/mnt"))
    pcb_browse_start: Path = PROJECT_ROOT
    # アップロード保存先。pcb_browse_root 配下かつ pcb_browse_allowed 内であること
    # （保存後にそのまま選択・閲覧できるようにするため）
    pcb_upload_dir: Path = PROJECT_ROOT / "uploads"
    # None の場合は machine_id（= hostname）ベースで解決する（routers/common.py）
    mainsail_url: str | None = None
    # backend の自己申告 ID。None なら socket.gethostname() を使う
    # （1 ホストに複数 backend を立てる E2E で区別するための注入口）
    hostname: str | None = None
    host: str = "0.0.0.0"
    port: int = 8080
    fake_camera: bool = False
    fake_camera_image: Path = (
        PROJECT_ROOT / "data" / "testing" / "webui" / "fake_camera.png"
    )

    @property
    def webui_data_dir(self) -> Path:
        """Backend WebAPI が所有する永続データのルート.

        ディレクトリ名は ``webui`` のまま保つ（実機の選択 PCB・基板別塗布 override・
        ジョブ成果物がこの下にあり、改名すると丸ごと孤立する）。
        """
        return self.data_dir / "webui"

    @classmethod
    def from_env(cls) -> Settings:
        """環境変数を反映した Settings を生成する.

        対応する環境変数:
            PCBASM_API_DATA_DIR, PCBASM_API_PCB_ROOT,
            PCBASM_MAINSAIL_URL, PCBASM_API_PORT,
            PCBASM_API_FAKE_CAMERA（"1" で固定画像カメラを使用）,
            PCBASM_API_FAKE_CAMERA_IMAGE

        ``PCBASM_API_PCB_ROOT`` を与えたときは、その root を
        ``pcb_browse_allowed`` にも追加する（root をわざわざ差し替える運用は
        「そこを見せる」意図しかなく、追加しないとファイルブラウザが全パス 400 に
        なる）。既定の 3 パスはそのまま残す。

        ``config_dir`` は既定値の生成時点で ``PCBASM_CONFIG_DIR`` を解決するため
        （``pcbasm.config.get_config_dir``）ここでは扱わない。

        Raises:
            ValueError: PCBASM_API_PORT が整数として解釈できない場合
        """
        base = cls()
        port_env = os.environ.get("PCBASM_API_PORT")
        pcb_root_env = os.environ.get("PCBASM_API_PCB_ROOT")
        pcb_browse_root = Path(pcb_root_env) if pcb_root_env else base.pcb_browse_root
        return cls(
            data_dir=_env_path("PCBASM_API_DATA_DIR", base.data_dir),
            pcb_browse_root=pcb_browse_root,
            pcb_browse_allowed=(
                (*base.pcb_browse_allowed, pcb_browse_root)
                if pcb_root_env
                else base.pcb_browse_allowed
            ),
            mainsail_url=os.environ.get("PCBASM_MAINSAIL_URL", base.mainsail_url),
            port=int(port_env) if port_env else base.port,
            fake_camera=os.environ.get("PCBASM_API_FAKE_CAMERA") == "1",
            fake_camera_image=_env_path(
                "PCBASM_API_FAKE_CAMERA_IMAGE", base.fake_camera_image
            ),
        )
