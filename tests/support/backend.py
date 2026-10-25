"""テスト用に Backend の装置とジョブ実行基盤を組む."""

from pathlib import Path

from pcbasm.hal import AudioPlayer
from pcbasm.machine.devices import Devices, open_devices
from pcbasm.pasting.overrides import BoardOverrides
from web.api.backend import Backend
from web.api.env import Env
from web.api.jobs.catalog import default_catalog
from web.api.jobs.pasting import PasteStores
from web.api.jobs.runtime.catalog import JobCatalog
from web.api.jobs.runtime.manager import JobManager
from web.api.machine_config import MachineConfigFile


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


def standalone_catalog(data_dir: Path) -> JobCatalog:
    """Backend を組まずに作る全ジョブのカタログ（定義だけを見るテスト用。塗布の永続データは ``data_dir`` の下）."""
    return default_catalog(
        PasteStores(
            overrides=BoardOverrides(data_dir),
            paste_dataset_dir=data_dir / "paste-dataset",
            paste_volume_calibration_dir=data_dir / "paste-volume-calibrations",
        ),
        data_dir / "machine_check.json",
    )
