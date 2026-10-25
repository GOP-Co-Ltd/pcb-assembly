"""UI frontend の起動設定（環境変数は port、fake、machines.toml を置く config の場所だけ）."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import attrs

from pcbasm import paths
from pcbasm.paths import PROJECT_ROOT
from web.contract.discovery import SERVICE_TYPE
from web.process import env_fake
from web.ui.machines import DEFAULT_BACKEND_PORT, MachineEndpoint


@attrs.frozen
class Settings:
    """UI frontend サーバーの設定値.

    backend WebAPI の ``Env`` と違い ``config_dir`` / ``data_dir`` / PCB ブラウザ /
    カメラの設定を一切持たない。frontend が config の場所から読むのは machines.toml だけで、
    装置に関する値はすべて backend から取得するため、機体でないホストでも起動できる。
    同居機で backend の ``data/webui`` を共有しないのも同じ理由。
    """

    host: str = "0.0.0.0"
    # 同居機では 8080 = UI / 8081 = API に分ける
    port: int = 8080
    # frontend 自身のホスト名。`/` はこれと同じ machine_id の機体（同居機）を開く
    hostname: str = attrs.field(factory=socket.gethostname)
    # 静的登録のマシン。実運用では machines_file から読んだ分をアプリ側で足す
    # （テストは直接注入する）
    machines: tuple[MachineEndpoint, ...] = ()
    machines_file: Path = PROJECT_ROOT / "config" / "machines.toml"
    # 1 ページの SSR に必要な backend 取得へ許す時間（超えたら 503 ページ）
    ssr_timeout: float = 2.0
    backend_connect_timeout: float = 2.0
    # プロキシが上流の応答を待つ上限。POST /api/machine/command は M400 待ちで
    # 最大 60s かかるので、それより長くする（MJPEG は無制限で別扱い）
    proxy_read_timeout: float = 120.0
    # machines.toml で port を省略したマシンに使う backend の port
    default_backend_port: int = DEFAULT_BACKEND_PORT
    # mDNS で LAN 上の backend を探索するか（静的登録と併設）
    discovery_enabled: bool = True
    discovery_service_type: str = SERVICE_TYPE
    # None なら zeroconf 既定（全 IF）。テストは ("127.0.0.1",) に限定する
    discovery_interfaces: tuple[str, ...] | None = None
    # frontend 自身のソフトウェア更新（frontend 専用機はここからしか更新できない）
    update_enabled: bool = True
    # リポジトリ直下に固定する。ロックで保護する対象は worktree なので、同居機の backend
    # （`web.api.env.Env.update_state_dir`）と必ず同じロックファイルを使う
    update_state_dir: Path = PROJECT_ROOT / "data" / "selfupdate"

    @classmethod
    def from_env(cls) -> Settings:
        """環境変数を反映した Settings を生成する.

        対応する環境変数:
            PCBASM_UI_PORT,
            PCBASM_CONFIG_DIR（machines.toml の場所。未設定ならリポジトリ直下の config/）,
            PCBASM_FAKE（"1" で mDNS 探索と自己更新を切る）

        Raises:
            ValueError: PCBASM_UI_PORT が整数として解釈できない場合
        """
        fake = env_fake()
        # fake の機体だけを操作させる（実 LAN の機体を探さず、更新もしない）
        settings = cls(
            machines_file=paths.config_dir() / "machines.toml",
            discovery_enabled=not fake,
            update_enabled=not fake,
        )
        port = os.environ.get("PCBASM_UI_PORT")
        return attrs.evolve(settings, port=int(port)) if port else settings
