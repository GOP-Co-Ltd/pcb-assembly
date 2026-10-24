"""Backend WebAPI の起動時の環境。環境変数による上書きをサポートする."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import attrs

from pcbasm.config import get_config_dir
from pcbasm.paths import PROJECT_ROOT
from web.contract.discovery import SERVICE_TYPE
from web.process import env_fake, env_path, env_words


@attrs.frozen
class Env:
    """Backend WebAPI サーバーの起動時の環境."""

    config_dir: Path
    data_dir: Path
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
    # Klipper とカメラを fake にする（`make dev` のシミュレーション起動）
    fake: bool = False
    # 与えるとカメラをこの画像を返し続ける fake にする（テストの注入口。env には出さない）
    fake_camera_image: Path | None = None
    # mDNS でこの backend を広告するか（frontend の自動発見用）
    discovery_enabled: bool = True
    # 広告するアドレス。None なら実 IF から列挙する（discovery.local_ipv4_addresses）
    advertise_addresses: tuple[str, ...] | None = None
    discovery_service_type: str = SERVICE_TYPE
    # None なら zeroconf 既定（全 IF）。テストは ("127.0.0.1",) で閉じる
    discovery_interfaces: tuple[str, ...] | None = None
    # WebUI からのソフトウェア更新（git pull → uv sync → 再起動）。無効にすると
    # 実行系が 403 を返し、ページから操作できなくなる
    update_enabled: bool = True
    # 素の `uv sync` は dependency group を削除し、lock がずれると uv.lock を書き換える
    # （web.selfupdate.settings のコメント参照）。機体ごとに増やせるよう env に出す
    update_uv_sync_args: tuple[str, ...] = ("--locked", "--inexact")
    # 自己更新の report と単一実行ロックの置き場所。data_dir からは導出しない。
    # ロックが守る対象は worktree なので、同居機の api と ui が
    # PCBASM_API_DATA_DIR の設定に関係なく同じロックを使う必要がある
    update_state_dir: Path = PROJECT_ROOT / "data" / "selfupdate"

    @property
    def webui_data_dir(self) -> Path:
        """Backend WebAPI が所有する永続データのルート.

        ディレクトリ名は ``webui`` のまま保つ（実機の選択 PCB・基板別塗布 override・
        ジョブ成果物がこの下にあり、改名するとこれらをすべて参照できなくなる）。
        """
        return self.data_dir / "webui"

    @property
    def journal_dir(self) -> Path:
        """G-code ジャーナルの置き場所（成果物と同じく ``/artifacts`` から配信する）."""
        return self.webui_data_dir / "journal"

    @property
    def paste_dataset_dir(self) -> Path:
        """ペースト塗布画像 dataset の永続保存先."""
        return self.data_dir / "paste-volume-datasets"

    @property
    def paste_volume_calibration_dir(self) -> Path:
        """直径ベース塗布量校正ファイルの保存先.

        校正は装置の設定と同じく再現に要る資産なので Git 管理する（``data/.gitignore``
        は特定 directory だけを除外しており、ここは対象外）。
        """
        return self.data_dir / "paste-volume-calibrations"

    @classmethod
    def from_env(cls) -> Env:
        """環境変数を反映した Env を生成する.

        config_dir と data_dir の既定はリポジトリ直下（本番の unit は env を設定しない）。

        対応する環境変数:
            PCBASM_CONFIG_DIR, PCBASM_API_DATA_DIR, PCBASM_API_PCB_ROOT,
            PCBASM_MAINSAIL_URL, PCBASM_API_PORT,
            PCBASM_FAKE（"1" で Klipper とカメラを fake にし、mDNS 広告と WebUI からの更新を切り、アップロード先を data_dir に移す）,
            PCBASM_API_DISCOVERY_ENABLED（"0" で mDNS 広告を無効）,
            PCBASM_API_UPDATE_ENABLED（"0" で WebUI からの更新を無効）,
            PCBASM_API_UPDATE_UV_SYNC_ARGS（空白区切り。`uv sync` の引数を丸ごと置換）,
            PCBASM_API_UPDATE_STATE_DIR

        ``discovery_service_type`` / ``discovery_interfaces`` /
        ``advertise_addresses`` / ``fake_camera_image`` は env に出さない（テストと E2E は
        コンストラクタ注入で足りるため、使われない設定項目を増やさない）。

        ``PCBASM_API_PCB_ROOT`` を与えたときは、その root を
        ``pcb_browse_allowed`` にも追加する（root をわざわざ差し替える運用は
        「そこを見せる」意図しかなく、追加しないとファイルブラウザが全パス 400 に
        なる）。既定の 3 パスはそのまま残す。

        Raises:
            ValueError: PCBASM_API_PORT が整数として解釈できない場合
        """
        base = cls(
            config_dir=get_config_dir(),
            data_dir=env_path("PCBASM_API_DATA_DIR", PROJECT_ROOT / "data"),
        )
        fake = env_fake()
        port_env = os.environ.get("PCBASM_API_PORT")
        pcb_root_env = os.environ.get("PCBASM_API_PCB_ROOT")
        pcb_browse_root = Path(pcb_root_env) if pcb_root_env else base.pcb_browse_root
        # fake の機体は実機と同じリポジトリで動きうるので、アップロードで実機の基板を上書きしない
        pcb_upload_dir = base.data_dir / "uploads" if fake else base.pcb_upload_dir
        return attrs.evolve(
            base,
            pcb_browse_root=pcb_browse_root,
            pcb_browse_allowed=(
                *base.pcb_browse_allowed,
                *((pcb_browse_root,) if pcb_root_env else ()),
                *((pcb_upload_dir,) if fake else ()),
            ),
            pcb_upload_dir=pcb_upload_dir,
            mainsail_url=os.environ.get("PCBASM_MAINSAIL_URL", base.mainsail_url),
            port=int(port_env) if port_env else base.port,
            fake=fake,
            # fake の機体は実機と同じ machine_id を名乗るので、実 LAN に広告しない
            discovery_enabled=not fake
            and os.environ.get("PCBASM_API_DISCOVERY_ENABLED") != "0",
            update_enabled=not fake
            and os.environ.get("PCBASM_API_UPDATE_ENABLED") != "0",
            update_uv_sync_args=env_words(
                "PCBASM_API_UPDATE_UV_SYNC_ARGS", base.update_uv_sync_args
            ),
            update_state_dir=env_path(
                "PCBASM_API_UPDATE_STATE_DIR", base.update_state_dir
            ),
        )


def resolve_machine_id(env: Env) -> str:
    """Backend の自己申告 machine_id（``/api/machine-info`` と mDNS 広告で共有）.

    ここ 1 箇所でだけ導出する。2 箇所で別々に導出すると ``hostname`` を注入した
    E2E で広告 ID と ``/api/machine-info`` の ID がずれ、frontend の
    ``/m/{machine_id}`` が解決できなくなる。
    """
    return env.hostname or socket.gethostname()
