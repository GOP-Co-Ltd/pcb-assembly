"""テスト用に隔離した backend / frontend の Settings を組む.

E2E の実サーバーと、アプリを組んで route を調べるテスト（tests/repo）が共有する。
"""

from pathlib import Path

from tests.helpers import copy_testing_config
from tests.web.api.conftest import FAKE_CAMERA_IMAGE
from web.api.env import Settings
from web.ui.machines import MachineEndpoint
from web.ui.settings import Settings as UiSettings


def make_api_settings(root: Path, *, hostname: str) -> Settings:
    """Fake カメラ + テスト用 config + 隔離 data_dir の backend Settings を組む.

    `data/testing/config` を `root` 内に複製する。Klipper port 7126（非リッスン）なので、
    誤って実機 Moonraker に接続しない。

    Args:
        root: config / data / pcb root を置く隔離ディレクトリ（無ければ作る）
        hostname: backend の自己申告 machine_id。1 ホストに複数 backend を立てる
            テスト（test_update_e2e.py 等）で URL prefix を区別するために注入する

    Returns:
        構築済みの backend Settings
    """
    root.mkdir(parents=True, exist_ok=True)
    config_dir = copy_testing_config(root)
    pcb_root = root / "pcb"
    pcb_root.mkdir()
    data_dir = root / "data"
    data_dir.mkdir()
    return Settings(
        config_dir=config_dir,
        data_dir=data_dir,
        pcb_browse_root=pcb_root,
        # 公開範囲は既定（リポジトリ + /media + /mnt）から暗黙に広がらないため、
        # tmp の pcb root を明示的に許可する
        pcb_browse_allowed=(pcb_root,),
        pcb_browse_start=pcb_root,
        pcb_upload_dir=pcb_root / "uploads",
        mainsail_url="http://mainsail.invalid",
        hostname=hostname,
        fake_camera_image=FAKE_CAMERA_IMAGE,
        # 実 LAN へ mDNS を撒かない（広告 + 探索の通しは test_discovery_e2e.py が
        # ランダムなサービス型 + ループバック限定で見る）
        discovery_enabled=False,
        # 自己更新の report / ロックをリポジトリの data/ に落とさない
        update_state_dir=root / "selfupdate",
    )


def make_ui_settings(
    endpoints: tuple[MachineEndpoint, ...], *, machines_file: Path
) -> UiSettings:
    """静的登録だけを持つ frontend Settings を組む.

    Args:
        endpoints: 登録する backend（この順序が一覧とドロップダウンの順序になる）
        machines_file: 不在パスを渡す。リポジトリの `config/machines.toml` を
            テストが拾って実機の登録を混ぜないようにするため

    Returns:
        構築済みの frontend Settings
    """
    return UiSettings(
        machines=endpoints,
        machines_file=machines_file,
        discovery_enabled=False,
        # 自己更新の report / ロックをリポジトリの data/ に落とさない
        update_state_dir=machines_file.parent / "selfupdate",
    )
