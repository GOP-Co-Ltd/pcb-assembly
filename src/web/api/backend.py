"""Backend: 機体 1 台の WebAPI が持つサービスの型付きコンテナ.

FastAPI の ``app.state`` には ``backend`` だけを置き、router は ``web.api.deps`` を通して各サービスを受け取る。
"""

from __future__ import annotations

import logging
from pathlib import Path

import attrs
from shapely import Polygon

from pcbasm.align.scene import board_scene, placed_on_reference
from pcbasm.machine.devices import Devices, open_devices
from pcbasm.pasting.overrides import BoardOverrides
from pcbasm.pasting.test_board.generator import BoardGenerator
from web.api.camera import CameraFeed
from web.api.control import ControlLease, control_changed_event
from web.api.discovery import (
    ServiceAdvertiser,
    local_ipv4_addresses,
    select_advertise_addresses,
)
from web.api.env import Env, resolve_machine_id
from web.api.events import EventBus
from web.api.jobs.catalog import default_catalog
from web.api.jobs.pasting import PasteStores
from web.api.jobs.runtime.catalog import JobCatalog
from web.api.jobs.runtime.manager import JobManager
from web.api.lock import MachineLock
from web.api.machine_config import MachineConfigFile
from web.api.selection import Selection
from web.selfupdate.runner import UpdateRunner
from web.selfupdate.settings import UpdateSettings

logger = logging.getLogger(__name__)


@attrs.frozen
class Backend:
    """機体 1 台の WebAPI のサービス一式（:func:`open_backend` が組む）.

    Attributes:
        env: 起動時の環境
        machine_config: machine.toml の読み込みと書き込み
        devices: 装置（Klipper、カメラ、通知音）
        lock: 装置排他ロック
        selection: 選択中の PCB とジョブフォームの既定値
        camera: FrameHub の貸し出しと MJPEG 配信
        events: WS 購読者へのイベント配信
        catalog: ジョブ定義カタログ
        paste_stores: 塗布の永続データ（catalog の塗布ジョブと HTTP 経路が同じものを使う）
        jobs: ジョブの起動と直近 1 件の状態
        control: 操作権リース（自動解放しない。プロセス再起動で空きに戻る）
        board_generator: テスト塗布基板の生成
        update: 自己更新のランナー
        advertiser: mDNS 広告（無効なら None。開始は lifespan で行う）
    """

    env: Env
    machine_config: MachineConfigFile
    devices: Devices
    lock: MachineLock
    selection: Selection
    camera: CameraFeed
    events: EventBus
    catalog: JobCatalog
    paste_stores: PasteStores
    jobs: JobManager
    control: ControlLease
    board_generator: BoardGenerator
    update: UpdateRunner
    advertiser: ServiceAdvertiser | None

    def close(self) -> None:
        """シャットダウンの後始末（preview に終了を通知 → ジョブを join → 装置を閉じる）."""
        self.camera.request_shutdown()
        self.jobs.shutdown()
        self.devices.close()


def open_backend(
    env: Env,
    *,
    devices: Devices | None = None,
    paste_test_board_footprint_root: Path | None = None,
    update_runner: UpdateRunner | None = None,
) -> Backend:
    """``env`` の機体の Backend を組む（外部装置へは接続しない）.

    machine.toml を移行してから読む。
    設定エラーでも組み立ては止めず degraded で上げる（落とすと WebUI から直せない）。

    Args:
        env: 起動時の環境
        devices: 装置（None なら env どおりに組む。テストは fake を注入する）
        paste_test_board_footprint_root: テスト塗布基板で使う KiCad footprint root。None なら環境変数または KiCad 9 標準パス
        update_runner: 自己更新のランナー（None なら env から構築）。テストはスタブ実行ファイルを差した UpdateSettings 版を注入する

    Raises:
        RuntimeError: machine.toml が無い場合
    """
    events = EventBus()
    machine_config = MachineConfigFile(
        env.config_dir, on_change=events.publish_state_changed
    )
    if not machine_config.path.is_file():
        raise RuntimeError(
            f"machine.toml がありません: {machine_config.path} "
            f"（./scripts/setup-machine-config.sh を実行してください）"
        )
    machine_config.migrate()
    _, config_error = machine_config.machine_or_error()
    if config_error is not None:
        logger.warning("設定エラーのため degraded で起動します: %s", config_error)

    lock = MachineLock()
    if devices is None:
        # scene はカメラを開くときに呼ぶので、selection はそれまでに組み終わっている
        devices = open_devices(
            machine_config.path,
            env.journal_dir,
            fake=env.fake,
            scene=lambda: _scene(selection, machine_config),
            camera_image=env.fake_camera_image,
        )
    # fake のカメラに選び直した基板を映す
    selection = Selection(
        env, lock, on_select=devices.rebuild_camera if env.fake else lambda: None
    )
    camera = CameraFeed(devices, machine_config)
    paste_stores = PasteStores(
        overrides=BoardOverrides(env.webui_data_dir),
        paste_dataset_dir=env.paste_dataset_dir,
        paste_volume_calibration_dir=env.paste_volume_calibration_dir,
    )
    catalog = default_catalog(paste_stores, env.machine_check_file)
    control = ControlLease(on_change=lambda: events.publish(control_changed_event()))
    return Backend(
        env=env,
        machine_config=machine_config,
        devices=devices,
        lock=lock,
        selection=selection,
        camera=camera,
        events=events,
        catalog=catalog,
        paste_stores=paste_stores,
        jobs=JobManager(
            catalog,
            env=env,
            machine_config=machine_config,
            devices=devices,
            lock=lock,
            selection=selection,
            camera=camera,
            events=events,
        ),
        control=control,
        board_generator=BoardGenerator(paste_test_board_footprint_root),
        update=update_runner if update_runner is not None else _update_runner(env),
        advertiser=_advertiser(env) if env.discovery_enabled else None,
    )


def _scene(selection: Selection, machine_config: MachineConfigFile) -> list[Polygon]:
    """Fake のカメラに映す、選択中の PCB を基準点どおりに置いた基準円と銅箔."""
    selected = selection.selected_board()
    if selected is None:
        return []
    _, board = selected
    reference = machine_config.machine().reference_point
    placement = placed_on_reference(reference)
    return board_scene(board, reference, placement, placement)


def _advertiser(env: Env) -> ServiceAdvertiser:
    """環境から広告を組む（ソケットは開かない）.

    設定エラー（degraded）でも広告する。機体一覧に載らないと WebUI から直せない。

    フィルタ（`select_advertise_addresses`）を掛けるのは実 IF から列挙したときだけ。
    ``advertise_addresses`` の注入はそのまま使う（loopback を除外すると、この注入口の
    目的である「テストをループバックに閉じる」が成立しない）。
    """
    addresses = env.advertise_addresses
    if addresses is None:
        addresses = select_advertise_addresses(local_ipv4_addresses())
    return ServiceAdvertiser(
        machine_id=resolve_machine_id(env),
        port=env.port,
        addresses=addresses,
        service_type=env.discovery_service_type,
        interfaces=env.discovery_interfaces,
    )


def _update_runner(env: Env) -> UpdateRunner:
    """環境から自己更新のランナーを組む（リポジトリは常にこのソースツリー）.

    ブランチ・remote・`uv` の引数はサーバ側の固定値。リクエストからは変更できない。
    """
    return UpdateRunner(
        UpdateSettings(
            state_dir=env.update_state_dir,
            uv_sync_args=env.update_uv_sync_args,
            enabled=env.update_enabled,
        )
    )
