"""ジョブの golden を採る実行環境（1 台の fake 機体、それを映すカメラ、ジョブの実行基盤）.

ジョブは実 JobManager で走らせる。
``open_klipper``（PR02 の生成口）は、何度呼ばれても同じ fake 機体への接続を返す。
カメラは AppState が組むものを差し替える（カメラの生成口は AppState の中にしか無い）。
"""

import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from shapely import Point, Polygon

from pcbasm import hal
from pcbasm.config import Corner, Machine
from pcbasm.geometry import Compose, Point2d, Rotation, Shift, transform_polygon
from pcbasm.hal import Camera
from pcbasm.hal.fake import FakeAudioPlayer, FakeKlipper
from pcbasm.pcb import Layer, PcbFile
from web.api import state as state_module
from web.api.board_settings import BoardSettingsStore
from web.api.config_store import ConfigStore
from web.api.jobs.catalog import JobCatalog, default_catalog
from web.api.jobs.manager import JobManager
from web.api.preview import PreviewService
from web.api.settings import Settings
from web.api.state import AppState

GOLDEN_DIR = Path(__file__).parent
CONFIG_FILES = (GOLDEN_DIR / "machine.toml", GOLDEN_DIR / "camera_calibration.json")

# 基板の実際の置き方（board → 機械座標）。左上の基準円は reference_point から 0.3 mm ほどずれる
BOARD_PLACEMENT = Compose([Rotation(0.3), Shift(6.2, 10.5)])
# 基準円に対する銅箔のずれ（銅箔照合が測る変位）
COPPER_OFFSET = Shift(0.04, -0.03)


def board_shapes(pcb: PcbFile, machine: Machine) -> list[Polygon]:
    """基板を BOARD_PLACEMENT に置いたときの、4 隅の基準円と TOP 銅箔を機械座標で返す."""
    reference = machine.reference_point
    width, height = pcb.outline.width, pcb.outline.height
    corners = {
        Corner.TOP_LEFT: Point2d(0.0, 0.0),
        Corner.TOP_RIGHT: Point2d(width, 0.0),
        Corner.BOTTOM_RIGHT: Point2d(width, height),
        Corner.BOTTOM_LEFT: Point2d(0.0, height),
    }
    markers = [
        BOARD_PLACEMENT.apply(corner + reference.offsets.get(name))
        for name, corner in corners.items()
    ]
    copper = Compose([BOARD_PLACEMENT, COPPER_OFFSET])
    return [
        *(
            Point(marker.x, marker.y).buffer(reference.target_diameter / 2, 32)
            for marker in markers
        ),
        *(
            transform_polygon(island.polygon, copper)
            for island in pcb.copper
            if island.layer == Layer.TOP
        ),
    ]


@pytest.fixture
def machine_klipper(monkeypatch: pytest.MonkeyPatch) -> FakeKlipper:
    """``open_klipper`` がどれも接続を返す 1 台の fake 機体（送信の記録は全接続で 1 列）."""
    klipper = FakeKlipper()
    monkeypatch.setattr(
        hal,
        "open_klipper",
        lambda config, *, timeout=None, journal=None: klipper.connection(
            timeout=timeout
        ),
    )
    return klipper


@pytest.fixture
def mount_camera(monkeypatch: pytest.MonkeyPatch) -> Callable[[Camera], None]:
    """AppState がジョブに貸すカメラを差し替える関数を返す."""

    def mount(camera: Camera) -> None:
        monkeypatch.setattr(state_module, "create_camera", lambda **kwargs: camera)

    return mount


@pytest.fixture
def job_settings(tmp_path: Path) -> Settings:
    """テスト用の config（machine.toml と校正ファイル）と、空の PCB 置き場."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for path in CONFIG_FILES:
        shutil.copy(path, config_dir)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    pcb_root = tmp_path / "pcb"
    pcb_root.mkdir()
    return Settings(
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
def job_state(job_settings: Settings) -> Iterator[AppState]:
    state = AppState(job_settings, ConfigStore(job_settings.config_dir))
    yield state
    state.close()


@pytest.fixture
def catalog() -> JobCatalog:
    return default_catalog()


@pytest.fixture
def manager(
    job_state: AppState, job_settings: Settings, catalog: JobCatalog
) -> Iterator[JobManager]:
    manager = JobManager(
        job_state,
        PreviewService(job_state),
        catalog,
        job_settings,
        BoardSettingsStore(job_settings.webui_data_dir),
        audio_player=FakeAudioPlayer(),
    )
    yield manager
    manager.shutdown()
