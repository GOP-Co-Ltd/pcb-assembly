"""ジョブの golden を採る実行環境（1 台の fake 機体、それを映すカメラ、ジョブの実行基盤）.

ジョブは実 Backend の上の実 JobManager で走らせる。
装置は fake の機体・カメラ・通知音を持つ ``Devices`` を注入する。
``open_klipper`` は塞いだままなので、``Devices`` を通らずに Klipper を作る経路があればテストが落ちる。
"""

import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from shapely import Polygon

from pcbasm.hal import Camera
from pcbasm.hal.fake import FakeAudioPlayer, FakeKlipper
from pcbasm.kicad import load_board
from pcbasm.kicad.build import generate_grid_pcb
from pcbasm.machine.devices import Devices
from tests.support.golden import CONFIG_FILES, board_shapes
from tests.support.paths import LED_BLINKER_PCB
from tests.support.web import job_manager
from web.api.backend import Backend, open_backend
from web.api.env import Env
from web.api.jobs.runtime.catalog import JobCatalog
from web.api.jobs.runtime.manager import JobManager
from web.api.machine_config import MachineConfigFile


@pytest.fixture
def machine_klipper() -> FakeKlipper:
    """Golden を採る 1 台の fake 機体（ジョブも route も ``Devices`` を通してこれへ送る）."""
    return FakeKlipper()


@pytest.fixture
def mounted_cameras() -> list[Camera]:
    return []


@pytest.fixture
def mount_camera(mounted_cameras: list[Camera]) -> Callable[[Camera], None]:
    """装置がジョブに貸すカメラを差し替える関数を返す（カメラを開くときに最後に置いたものを使う）."""
    return mounted_cameras.append


@pytest.fixture
def devices(
    job_settings: Env,
    machine_klipper: FakeKlipper,
    mounted_cameras: list[Camera],
    audio_player: FakeAudioPlayer,
) -> Devices:
    return Devices(
        MachineConfigFile(job_settings.config_dir).path,
        job_settings.journal_dir,
        klipper=machine_klipper,
        camera=lambda: mounted_cameras[-1],
        audio=audio_player,
    )


@pytest.fixture
def job_settings(tmp_path: Path) -> Env:
    """テスト用の config（machine.toml と校正ファイル）と、空の PCB 置き場."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for path in CONFIG_FILES:
        shutil.copy(path, config_dir)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    pcb_root = tmp_path / "pcb"
    pcb_root.mkdir()
    return Env(
        config_dir=config_dir,
        data_dir=data_dir,
        pcb_browse_root=pcb_root,
        pcb_browse_allowed=(pcb_root,),
        pcb_browse_start=pcb_root,
        pcb_upload_dir=pcb_root / "uploads",
        discovery_enabled=False,
        update_state_dir=tmp_path / "selfupdate",
    )


@pytest.fixture
def job_backend(job_settings: Env, devices: Devices) -> Iterator[Backend]:
    backend = open_backend(job_settings, devices=devices)
    yield backend
    backend.close()


@pytest.fixture
def place_board(
    job_settings: Env, job_backend: Backend
) -> Callable[[Path], list[Polygon]]:
    """PCB を選び、置いた基板の基準円と銅箔を返す関数を返す."""

    def place(pcb: Path) -> list[Polygon]:
        shutil.copy(pcb, job_settings.pcb_browse_root)
        job_backend.selection.select(Path(pcb.name))
        return board_shapes(load_board(pcb), job_backend.machine_config.machine())

    return place


@pytest.fixture(scope="session")
def grid_pcb(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """2x2 グリッド PCB（20 mm 角、pad の辺 0.4 mm は pad 中心照合の対象）."""
    path = tmp_path_factory.mktemp("grid") / "grid.kicad_pcb"
    generate_grid_pcb(20.0, 2, 0.4, path)
    return path


@pytest.fixture
def grid_shapes(
    grid_pcb: Path, place_board: Callable[[Path], list[Polygon]]
) -> list[Polygon]:
    """グリッド PCB を選び、置いた基板の基準円と銅箔を返す."""
    return place_board(grid_pcb)


@pytest.fixture
def led_blinker_shapes(place_board: Callable[[Path], list[Polygon]]) -> list[Polygon]:
    """led_blinker（高さ計測できる TOP のベタを持つ）を選び、置いた基板の基準円と銅箔を返す."""
    return place_board(LED_BLINKER_PCB)


@pytest.fixture
def catalog(job_backend: Backend) -> JobCatalog:
    return job_backend.catalog


@pytest.fixture
def audio_player() -> FakeAudioPlayer:
    """ジョブが鳴らす通知音を記録する（入力待ち音でオペレータ待ちに入ったと分かる）."""
    return FakeAudioPlayer()


@pytest.fixture
def manager(job_backend: Backend, catalog: JobCatalog) -> Iterator[JobManager]:
    manager = job_manager(job_backend, catalog)
    yield manager
    manager.shutdown()
