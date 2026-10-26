"""Backend と frontend をテスト用に組む部品（隔離した設定、fake の装置、合成ジョブ、MJPEG の読み取り、mDNS）."""

import shutil
import socket
import struct
import threading
from collections.abc import Callable, Collection
from functools import cache, wraps
from pathlib import Path
from secrets import token_hex

import cv2
import numpy as np
import pytest

from pcbasm.config import CAPABILITIES, Capability
from pcbasm.hal import AudioPlayer
from pcbasm.machine.devices import Devices, open_devices
from pcbasm.pasting.overrides import BoardOverrides
from pcbasm.vision import ImageArray
from tests.support import polling
from tests.support.paths import FAKE_CAMERA_IMAGE, TEST_CONFIG_DIR
from web.api.backend import Backend
from web.api.env import Env
from web.api.jobs.catalog import default_catalog
from web.api.jobs.pasting import PasteStores
from web.api.jobs.runtime.catalog import JobCatalog, JobDefinition, ParamSpec
from web.api.jobs.runtime.context import JobContext
from web.api.jobs.runtime.manager import JobManager
from web.api.jobs.runtime.record import JobRecord, JobResult, PromptSpec
from web.api.machine_config import MachineConfigFile
from web.ui.machines import MachineEndpoint
from web.ui.settings import Settings as UiSettings

# frontend の登録 id と backend の自己申告 id（SSR ページが URL の machine_id と突き合わせる）
BACKEND_MACHINE_ID = "uitest"

type ManagerFactory = Callable[..., JobManager]


def copy_test_config(root: Path) -> Path:
    """`tests/data/config` を ``root/config`` へ複製して返す.

    テストが machine.toml を書き換えても、追跡下の素材を汚さない。
    """
    config_dir = root / "config"
    shutil.copytree(TEST_CONFIG_DIR, config_dir)
    return config_dir


def make_api_settings(root: Path, *, hostname: str) -> Env:
    """Fake カメラ + テスト用 config + 隔離 data_dir の backend Env を組む.

    tests/data/config を ``root`` 内に複製する。Klipper は非リッスンの port 7126 なので、誤って実機の Moonraker に接続しない。

    Args:
        root: config / data / pcb root を置く隔離ディレクトリ（無ければ作る）
        hostname: backend の自己申告 machine_id。1 ホストに複数 backend を立てる
            テスト（test_update_e2e.py 等）で URL prefix を区別するために注入する

    Returns:
        構築済みの backend Env
    """
    root.mkdir(parents=True, exist_ok=True)
    config_dir = copy_test_config(root)
    pcb_root = root / "pcb"
    pcb_root.mkdir()
    data_dir = root / "data"
    data_dir.mkdir()
    return Env(
        config_dir=config_dir,
        data_dir=data_dir,
        pcb_browse_root=pcb_root,
        # 公開範囲は既定（リポジトリ + /media + /mnt）から暗黙に広がらないため、
        # tmp の pcb root を明示的に許可する
        pcb_browse_allowed=(pcb_root,),
        pcb_browse_start=pcb_root,
        pcb_upload_dir=pcb_root / "uploads",
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


def devices_with_audio(env: Env, audio: AudioPlayer) -> Devices:
    """``env`` どおりの装置で、通知音だけ ``audio`` にしたもの（``env.fake`` の機体は組まない）.

    既定の ALSA のままだと、ジョブの完了・応答待ちが実 `aplay` を起動して実スピーカーが鳴る。

    fake のカメラに選択中の基板を映す配線は open_backend が持つので、fake の機体は open_backend に組ませる。
    """
    assert not env.fake, "fake の機体は open_backend に装置を組ませる"
    return open_devices(
        MachineConfigFile(env.config_dir).path,
        env.journal_dir,
        fake=False,
        camera_image=env.fake_camera_image,
        audio=audio,
    )


def job_manager(
    backend: Backend, catalog: JobCatalog, *, log_capacity: int = 500
) -> JobManager:
    """``backend`` の装置・ロック・選択・カメラ・イベントを共有し、``catalog`` のジョブを走らせる JobManager."""
    return JobManager(
        catalog,
        env=backend.env,
        machine_config=backend.machine_config,
        devices=backend.devices,
        lock=backend.lock,
        selection=backend.selection,
        camera=backend.camera,
        events=backend.events,
        log_capacity=log_capacity,
    )


def standalone_catalog(
    data_dir: Path, capabilities: Collection[Capability] = CAPABILITIES
) -> JobCatalog:
    """Backend を組まずに作るカタログ（定義だけを見るテスト用）.

    既定は全機能の全ジョブ。塗布の永続データは ``data_dir`` の下に置く。
    """
    return default_catalog(
        capabilities,
        PasteStores(
            overrides=BoardOverrides(data_dir),
            paste_dataset_dir=data_dir / "paste-dataset",
            paste_volume_calibration_dir=data_dir / "paste-volume-calibrations",
        ),
        data_dir / "machine_check.json",
    )


def answer_next_prompt(
    record: JobRecord,
    manager: JobManager,
    answer: object,
    answered: set[str],
    *,
    timeout: float = 60.0,
) -> PromptSpec:
    """未応答の prompt を待って answer を返し、その prompt を返す（応答済み id は answered で管理）."""
    polling.wait_until(
        lambda: (pending := record.pending_prompt) is not None
        and pending[0] not in answered,
        timeout=timeout,
    )
    pending = record.pending_prompt
    assert pending is not None
    manager.respond_prompt(pending[0], answer)
    answered.add(pending[0])
    return pending[1]


def register_synthetic(
    catalog: JobCatalog,
    run: Callable[[JobContext], JobResult | None],
    *,
    name: str = "synthetic",
    label: str = "合成ジョブ",
    params: tuple[ParamSpec, ...] = (),
    requires_pcb: bool = False,
    uses_machine: bool = False,
    notify_on_completion: bool = False,
    accepts_commands: bool = False,
    persisted_params: tuple[str, ...] = (),
) -> None:
    """合成ジョブを catalog へ登録する（テスト専用の共通形）."""
    catalog.register(
        JobDefinition(
            name=name,
            label=label,
            run=run,
            params=params,
            requires_pcb=requires_pcb,
            uses_machine=uses_machine,
            notify_on_completion=notify_on_completion,
            accepts_commands=accepts_commands,
            persisted_params=persisted_params,
        )
    )


def register_gated(
    catalog: JobCatalog,
    *,
    name: str = "gated",
    uses_machine: bool = False,
    accepts_commands: bool = False,
    notify_on_completion: bool = False,
) -> threading.Event:
    """gate.set() で成功終了し、abort 要求は checkpoint で拾う合成ジョブを登録する."""
    gate = threading.Event()

    def run(ctx: JobContext) -> None:
        while not gate.wait(timeout=0.02):
            ctx.checkpoint()

    register_synthetic(
        catalog,
        run,
        name=name,
        uses_machine=uses_machine,
        accepts_commands=accepts_commands,
        notify_on_completion=notify_on_completion,
    )
    return gate


def jpeg_payload(part: bytes) -> bytes:
    """MJPEG の 1 パート（boundary 行 + ヘッダ + JPEG + CRLF）から JPEG bytes を取り出す."""
    _, _, body = part.partition(b"\r\n\r\n")
    return body.rstrip(b"\r\n")


def decode_jpeg(data: bytes) -> ImageArray | None:
    """JPEG bytes を BGR 配列に復号する（復号できなければ None）."""
    return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)


_MDNS_GROUP = "224.0.0.251"
_MDNS_PORT = 5353


@cache
def _mdns_available() -> bool:
    """5353 を共有 bind してループバックでマルチキャストに join できるか確認する.

    実機では avahi が 5353 を持っているので `SO_REUSEADDR` での共存を確かめる
    （共存できない環境ではテストが zeroconf を起動できない）。bind し直さないよう結果はキャッシュする。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("", _MDNS_PORT))
            probe.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_ADD_MEMBERSHIP,
                struct.pack(
                    "=4s4s",
                    socket.inet_aton(_MDNS_GROUP),
                    socket.inet_aton("127.0.0.1"),
                ),
            )
        return True
    except OSError:
        return False


def skip_if_no_mdns[**P, R](test: Callable[P, R]) -> Callable[P, R]:
    """テストを、mDNS が使えない環境では実行時に skip する."""

    @wraps(test)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        if not _mdns_available():
            pytest.skip("mDNS（5353 の共有 bind / マルチキャスト join）が使えません")
        return test(*args, **kwargs)

    return wrapper


def random_service_type() -> str:
    """テスト専用の DNS-SD サービス型（実 LAN / CI の並列ジョブと混ざらない）.

    実 zeroconf を触るテストは運用のサービス型（``_pcbasm._tcp``）を使わない。
    bind が失敗する前提のテスト（存在しない IF を指定するもの）でも、万一 bind が
    成功したときに実 LAN へ広告・探索を漏らさないための構造的な予防。
    """
    return f"_pcbasmt{token_hex(4)}._tcp.local."
