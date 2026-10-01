"""UI frontend の起動設定。env による上書きをサポートする."""

from __future__ import annotations

import os
from pathlib import Path

import attrs

from pcbasm.utils import PROJECT_ROOT
from web.api.discovery import SERVICE_TYPE
from web.ui.machines import DEFAULT_BACKEND_PORT, MachineEndpoint


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else default


def _env_words(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """空白区切りの env を argv 断片として読む（未設定なら既定）."""
    value = os.environ.get(name)
    return tuple(value.split()) if value else default


@attrs.frozen
class Settings:
    """UI frontend サーバーの設定値.

    backend WebAPI の ``Settings`` と違い ``config_dir`` / ``data_dir`` / PCB ブラウザ /
    カメラの設定を一切持たない。frontend は machine.toml を読まず（``get_config_dir()``
    を呼ばず）、装置に関する値はすべて backend から取得するため、機体でないホストでも
    起動できる。同居機で backend の ``data/webui`` を共有しないのも同じ理由。
    """

    host: str = "0.0.0.0"
    # 同居機では 8080 = UI / 8081 = API に分ける
    port: int = 8080
    # 静的登録のマシン。実運用では machines_file から読んだ分をアプリ側で足す
    # （テストや ui-fake は直接注入する）
    machines: tuple[MachineEndpoint, ...] = ()
    machines_file: Path = PROJECT_ROOT / "config" / "machines.toml"
    # 1 ページの SSR に必要な backend 取得へ許す時間（超えたら 503 ページ）
    ssr_timeout: float = 2.0
    backend_connect_timeout: float = 2.0
    # プロキシが上流の応答を待つ上限。POST /api/machine-control は M400 待ちで
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
    update_uv_sync_args: tuple[str, ...] = ("--locked", "--inexact")
    # リポジトリ直下に固定する。ロックで保護する対象は worktree なので、同居機の backend
    # （`web.api.settings.Settings.update_dir`）と必ず同じロックファイルを使う
    update_state_dir: Path = PROJECT_ROOT / "data" / "selfupdate"

    @classmethod
    def from_env(cls) -> Settings:
        """環境変数を反映した Settings を生成する.

        対応する環境変数:
            PCBASM_UI_HOST, PCBASM_UI_PORT, PCBASM_UI_MACHINES_FILE,
            PCBASM_UI_SSR_TIMEOUT, PCBASM_UI_BACKEND_CONNECT_TIMEOUT,
            PCBASM_UI_PROXY_READ_TIMEOUT, PCBASM_UI_DEFAULT_BACKEND_PORT,
            PCBASM_UI_DISCOVERY_ENABLED（"0" で mDNS 探索を無効）,
            PCBASM_UI_UPDATE_ENABLED（"0" で自己更新を無効）,
            PCBASM_UI_UPDATE_UV_SYNC_ARGS（空白区切り）, PCBASM_UI_UPDATE_STATE_DIR

        ``machines`` は env では扱わない（マシン一覧の情報源は ``machines_file`` と
        mDNS 探索）。``discovery_service_type`` / ``discovery_interfaces`` も env に出さない
        （テストと E2E はコンストラクタ注入で足りる）。

        Raises:
            ValueError: 数値の env が整数 / 実数として解釈できない場合
        """
        base = cls()
        port = os.environ.get("PCBASM_UI_PORT")
        ssr_timeout = os.environ.get("PCBASM_UI_SSR_TIMEOUT")
        connect_timeout = os.environ.get("PCBASM_UI_BACKEND_CONNECT_TIMEOUT")
        read_timeout = os.environ.get("PCBASM_UI_PROXY_READ_TIMEOUT")
        backend_port = os.environ.get("PCBASM_UI_DEFAULT_BACKEND_PORT")
        return cls(
            host=os.environ.get("PCBASM_UI_HOST", base.host),
            port=int(port) if port else base.port,
            machines_file=_env_path("PCBASM_UI_MACHINES_FILE", base.machines_file),
            ssr_timeout=float(ssr_timeout) if ssr_timeout else base.ssr_timeout,
            backend_connect_timeout=(
                float(connect_timeout)
                if connect_timeout
                else base.backend_connect_timeout
            ),
            proxy_read_timeout=(
                float(read_timeout) if read_timeout else base.proxy_read_timeout
            ),
            default_backend_port=(
                int(backend_port) if backend_port else base.default_backend_port
            ),
            discovery_enabled=os.environ.get("PCBASM_UI_DISCOVERY_ENABLED") != "0",
            update_enabled=os.environ.get("PCBASM_UI_UPDATE_ENABLED") != "0",
            update_uv_sync_args=_env_words(
                "PCBASM_UI_UPDATE_UV_SYNC_ARGS", base.update_uv_sync_args
            ),
            update_state_dir=_env_path(
                "PCBASM_UI_UPDATE_STATE_DIR", base.update_state_dir
            ),
        )
