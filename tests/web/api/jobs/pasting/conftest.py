"""塗布ジョブを登録した共通catalog。stateやmanagerは親のfixtureを使う。

計測済み `PasteSession` を fake HAL で組む道具もここに置く（合成ジョブへ実装を通す
検証で共用する）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
import shapely

from pcbasm.config import Machine
from pcbasm.geometry import Identity, Point2d, Shift
from pcbasm.hal import XYZStage
from pcbasm.pasting.alignment import PasteCorrection
from pcbasm.pasting.session import PasteSession
from pcbasm.pcb import PcbFile
from pcbasm.posctrl import (
    AlignmentRegion,
    BoardAlignment,
    BoardCalibrationResult,
    EdgeMatch,
    RegionAlignment,
)
from pcbasm.vision import Image, Offset
from pcbasm.vision.calibration import CalibrationResult
from tests.helpers import TESTING_CONFIG_DIR, TESTING_DATA_DIR, FakeCamera, FakeKlipper
from web.api.jobs.catalog import JobCatalog
from web.api.jobs.pasting import register_pasting_jobs

LED_BLINKER = TESTING_DATA_DIR / "led_blinker" / "led_blinker.kicad_pcb"
PPM = 10.0
RESOLUTION = (640, 480)
FOCUS_Z = 12.0


@pytest.fixture
def catalog() -> JobCatalog:
    """Pasting ジョブのみ登録した catalog（jobs/conftest の manager が使う）."""
    catalog = JobCatalog()
    register_pasting_jobs(catalog)
    return catalog


def uniform_frame() -> Image:
    """一様な銅板色のフレーム（塗布前後で同じ = はんだが写らない）."""
    width, height = RESOLUTION
    return Image(np.full((height, width, 3), 180, dtype=np.uint8))


def dot_frame(diameter_px: int) -> Image:
    """中心に暗い円を置いたフレーム（塗布後の 1 ドットを模す）."""
    width, height = RESOLUTION
    array = np.full((height, width, 3), 180, dtype=np.uint8)
    rows, columns = np.ogrid[:height, :width]
    disk = (rows - height / 2 + 0.5) ** 2 + (columns - width / 2 + 0.5) ** 2 <= (
        diameter_px / 2.0
    ) ** 2
    array[disk] = 40
    return Image(array)


def paste_session(camera: FakeCamera, klipper: FakeKlipper) -> PasteSession:
    """計測済みの `PasteSession`（board は原点から (100, 50) へ載っている）."""
    result = BoardCalibrationResult(
        machine=Machine(TESTING_CONFIG_DIR / "machine.toml"),
        klipper=klipper,
        stage=XYZStage(klipper.readonly),
        camera=camera,
        calibration=CalibrationResult(
            pixel_per_mm=PPM,
            square_size_mm=1.0,
            mean_distance_px=PPM,
            std_distance_px=0.0,
            resolution=RESOLUTION,
            crop_size=(400, 400),
            calibrated_at=datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC),
            z_position=FOCUS_Z,
        ),
        offset_transform=Identity(),
        board_transform=Shift(100.0, 50.0),
        pcb=PcbFile(LED_BLINKER),
    )
    return PasteSession.from_calibration(result)


def full_board_correction() -> PasteCorrection:
    """基板全面を覆う成功領域 1 つ（変位なし）の補正."""
    area = shapely.box(-100.0, -100.0, 100.0, 100.0)
    return PasteCorrection(
        alignment=BoardAlignment(
            results=(
                RegionAlignment(
                    region=AlignmentRegion(
                        index=0,
                        board_center=Point2d(0.0, 0.0),
                        anchor=Point2d(0.0, 0.0),
                        roi=(0, 0, 100, 100),
                        board_area=area,
                    ),
                    match=EdgeMatch(
                        offset=Offset(px=Point2d(0.0, 0.0), pixel_per_mm=PPM),
                        rms_distance_px=0.0,
                    ),
                    displacement=Point2d(0.0, 0.0),
                    increment=Point2d(0.0, 0.0),
                    passes=1,
                ),
            )
        ),
        height_plane=Identity(),
    )
