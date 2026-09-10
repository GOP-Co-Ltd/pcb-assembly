"""Backend WebAPI の起動設定。env による上書きをサポートする."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import attrs

from pcbasm.config import get_config_dir
from pcbasm.utils import PROJECT_ROOT
from web.api.discovery import SERVICE_TYPE


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
    # 同居機では 8080 = UI frontend / 8081 = backend WebAPI に分ける
    # （既存ブックマークの :8080 がそのまま UI に着地する）
    port: int = 8081
    fake_camera: bool = False
    fake_camera_image: Path = (
        PROJECT_ROOT / "data" / "testing" / "webui" / "fake_camera.png"
    )
    # mDNS でこの backend を広告するか（frontend の自動発見用）
    discovery_enabled: bool = True
    # 広告するアドレス。None なら実 IF から列挙する（discovery.local_ipv4_addresses）
    advertise_addresses: tuple[str, ...] | None = None
    discovery_service_type: str = SERVICE_TYPE
    # None なら zeroconf 既定（全 IF）。テストは ("127.0.0.1",) で閉じる
    discovery_interfaces: tuple[str, ...] | None = None

    @property
    def webui_data_dir(self) -> Path:
        """Backend WebAPI が所有する永続データのルート.

        ディレクトリ名は ``webui`` のまま保つ（実機の選択 PCB・基板別塗布 override・
        ジョブ成果物がこの下にあり、改名すると丸ごと孤立する）。
        """
        return self.data_dir / "webui"

    @property
    def paste_dataset_dir(self) -> Path:
        """ペースト塗布画像datasetの永続保存先."""
        return self.data_dir / "paste-volume-datasets"

    @property
    def paste_volume_calibration_dir(self) -> Path:
        """直径ベース塗布量校正ファイルの保存先.

        校正は装置の設定と同じく再現に要る資産なので Git 管理する（``data/.gitignore``
        は特定 directory だけを除外しており、ここは対象外）。
        """
        return self.data_dir / "paste-volume-calibrations"

    @classmethod
    def from_env(cls) -> Settings:
        """環境変数を反映した Settings を生成する.

        対応する環境変数:
            PCBASM_API_DATA_DIR, PCBASM_API_PCB_ROOT,
            PCBASM_MAINSAIL_URL, PCBASM_API_PORT,
            PCBASM_API_FAKE_CAMERA（"1" で固定画像カメラを使用）,
            PCBASM_API_FAKE_CAMERA_IMAGE,
            PCBASM_API_DISCOVERY_ENABLED（"0" で mDNS 広告を無効）

        ``discovery_service_type`` / ``discovery_interfaces`` /
        ``advertise_addresses`` は env に出さない（テストと E2E はコンストラクタ注入
        で足りるため、使われない設定項目を増やさない）。

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
            discovery_enabled=os.environ.get("PCBASM_API_DISCOVERY_ENABLED") != "0",
        )


def resolve_machine_id(settings: Settings) -> str:
    """Backend の自己申告 machine_id（``/api/machine-info`` と mDNS 広告で共有）.

    ここ 1 箇所でだけ導出する。2 箇所で別々に導出すると ``hostname`` を注入した
    E2E で広告 ID と ``/api/machine-info`` の ID がずれ、frontend の
    ``/m/{machine_id}`` が解決できなくなる。
    """
    return settings.hostname or socket.gethostname()
