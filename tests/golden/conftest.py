"""ジョブの golden を採る実行環境（1 台の fake 機体、それを映すカメラ、ジョブの実行基盤）.

ジョブは実 JobManager で走らせる。
装置は fake の機体とカメラを持つ ``Devices`` を注入する。
``open_klipper`` は塞いだままなので、``Devices`` を通らずに Klipper を作る経路があればテストが落ちる。
"""

import json
import shutil
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest
from shapely import Polygon

from pcbasm.align.scene import board_scene
from pcbasm.board import Board
from pcbasm.config import MachineConfig, load_machine
from pcbasm.geometry import Compose, Point2d, Rotation, Shift
from pcbasm.hal import Camera
from pcbasm.hal.fake import Checkerboard, FakeAudioPlayer, FakeKlipper, SceneCamera
from pcbasm.kicad import load_board
from pcbasm.kicad.build import generate_grid_pcb
from pcbasm.machine.devices import Devices
from pcbasm.pasting.hierarchy import PadHierarchy
from pcbasm.pasting.overrides import BoardOverrides
from pcbasm.pasting.paste_volume.calibration import (
    CALIBRATION_SUFFIX,
    parse_calibration,
    write_calibration,
)
from pcbasm.vision import CalibrationResult
from tests.support import polling
from tests.support.paths import TESTING_DATA_DIR
from web.api.jobs.catalog import JobCatalog, default_catalog
from web.api.jobs.manager import JobManager, JobRecord, JobStatus
from web.api.machine_config import MachineConfigFile
from web.api.preview import PreviewService
from web.api.settings import Settings
from web.api.state import AppState

GOLDEN_DIR = Path(__file__).parent
JOBS = GOLDEN_DIR / "jobs"
CONFIG_FILES = (GOLDEN_DIR / "machine.toml", GOLDEN_DIR / "camera_calibration.json")
CALIBRATION = CalibrationResult.load(GOLDEN_DIR / "camera_calibration.json")
DISPENSER = load_machine(GOLDEN_DIR / "machine.toml").paste_dispenser
LED_BLINKER = TESTING_DATA_DIR / "led_blinker" / "led_blinker.kicad_pcb"
CALIBRATION_PIN = TESTING_DATA_DIR / "schemas" / "paste_volume_calibration_v1.json"
# 運転時補正で塗る点（board 座標）。pad の無い下側のベタに、crop が重ならない間隔で置く
FLOW_POINTS = (Point2d(5.0, 20.0), Point2d(10.0, 20.0), Point2d(15.0, 20.0))

# 基板の実際の置き方（board → 機械座標）。左上の基準円は reference_point から 0.3 mm ほどずれる
BOARD_PLACEMENT = Compose([Rotation(0.3), Shift(6.2, 10.5)])
# 基準円に対する銅箔のずれ（銅箔照合が測る変位）
COPPER_OFFSET = Shift(0.04, -0.03)


def board_shapes(pcb: Board, machine: MachineConfig) -> list[Polygon]:
    """基板を BOARD_PLACEMENT に置いたときの、4 隅の基準円と TOP 銅箔を機械座標で返す."""
    return board_scene(
        pcb,
        machine.reference_point,
        BOARD_PLACEMENT,
        Compose([BOARD_PLACEMENT, COPPER_OFFSET]),
    )


def scene_camera(
    klipper: FakeKlipper,
    rotation: float,
    *,
    shapes: Sequence[Polygon] = (),
    checkerboard: Checkerboard | None = None,
) -> SceneCamera:
    """校正ファイルと machine.toml のとおりに、klipper の位置から見える画像を描くカメラ."""
    return SceneCamera(
        klipper,
        pixel_per_mm=CALIBRATION.pixel_per_mm,
        toolhead=Point2d(DISPENSER.toolhead.x, DISPENSER.toolhead.y),
        rotations_per_ul=DISPENSER.rotations_per_ul,
        rotation=rotation,
        size=CALIBRATION.resolution,
        shapes=shapes,
        checkerboard=checkerboard,
    )


def finish(
    state: AppState, record: JobRecord, status: JobStatus = JobStatus.SUCCEEDED
) -> None:
    """終了時の駐機が済む（装置のロックが外れる）まで待ち、ジョブの終わり方を確かめる."""
    polling.wait_until(
        lambda: record.status.terminal and state.busy_owner is None, timeout=120.0
    )
    assert record.status == status, record.error


def wait_operator(
    record: JobRecord, audio_player: FakeAudioPlayer, stage: str, calls: int = 1
) -> None:
    """``stage`` にいて、ジョブ開始からの入力待ち音が通算 ``calls`` 回鳴る（入口で滞留コマンドを捨て終える）まで待つ."""
    polling.wait_until(
        lambda: record.progress_stage == stage
        and sum(sound == "prompt" for sound, *_ in audio_player.played) >= calls,
        timeout=60.0,
    )


def enable_flow_calibration(settings: Settings, state: AppState, pcb: Path) -> None:
    """校正ファイルと基板ごとの測定点を置き、運転時補正を有効にする."""
    calibration, error = parse_calibration(
        json.loads(CALIBRATION_PIN.read_text(encoding="utf-8"))
    )
    assert calibration is not None, error
    name = f"golden{CALIBRATION_SUFFIX}"
    write_calibration(settings.paste_volume_calibration_dir / name, calibration)
    MachineConfigFile(settings.config_dir).write(
        {
            "paste_dispenser.volume_correction.calibration_file": name,
            "paste_dispenser.volume_correction.settle_seconds": 0.0,
        }
    )
    board = load_board(pcb)
    store = BoardOverrides(settings.webui_data_dir)
    model = store.load_or_init(pcb.name, state.machine().paste_dispenser)
    store.save(
        pcb.name,
        model.with_flow_calibration_points(FLOW_POINTS),
        board_signature=PadHierarchy.build(board.components, board.pads).signature(),
    )


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
    job_settings: Settings, machine_klipper: FakeKlipper, mounted_cameras: list[Camera]
) -> Devices:
    return Devices(
        MachineConfigFile(job_settings.config_dir).path,
        job_settings.journal_dir,
        klipper=machine_klipper,
        camera=lambda: mounted_cameras[-1],
    )


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
def job_state(job_settings: Settings, devices: Devices) -> Iterator[AppState]:
    state = AppState(job_settings, MachineConfigFile(job_settings.config_dir), devices)
    yield state
    state.close()


@pytest.fixture
def place_board(
    job_settings: Settings, job_state: AppState
) -> Callable[[Path], list[Polygon]]:
    """PCB を選び、置いた基板の基準円と銅箔を返す関数を返す."""

    def place(pcb: Path) -> list[Polygon]:
        shutil.copy(pcb, job_settings.pcb_browse_root)
        job_state.select_pcb(Path(pcb.name))
        return board_shapes(load_board(pcb), job_state.machine())

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
    return place_board(LED_BLINKER)


@pytest.fixture
def catalog() -> JobCatalog:
    return default_catalog()


@pytest.fixture
def audio_player() -> FakeAudioPlayer:
    """ジョブが鳴らす通知音を記録する（入力待ち音でオペレータ待ちに入ったと分かる）."""
    return FakeAudioPlayer()


@pytest.fixture
def manager(
    job_state: AppState,
    job_settings: Settings,
    catalog: JobCatalog,
    audio_player: FakeAudioPlayer,
) -> Iterator[JobManager]:
    manager = JobManager(
        job_state,
        PreviewService(job_state),
        catalog,
        job_settings,
        BoardOverrides(job_settings.webui_data_dir),
        audio_player=audio_player,
    )
    yield manager
    manager.shutdown()
