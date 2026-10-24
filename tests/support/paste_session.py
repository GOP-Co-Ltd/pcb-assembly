"""計測済みの `PasteSession` を fake HAL で組む道具と、手順の経過を記録する hooks.

塗布の手順（`pcbasm.pasting` のパージ確認・運転時補正など）を装置なしで通す検証で共用する。
"""

from __future__ import annotations

import attrs
import numpy as np
import shapely

from pcbasm.align import (
    AlignmentRegion,
    BoardCorrection,
    BoardFrame,
    EdgeMatch,
    RegionFit,
)
from pcbasm.config import MachineConfig, load_machine
from pcbasm.geometry import Identity, Point2d, Shift
from pcbasm.hal import XYZStage
from pcbasm.hal.fake import FakeCamera, FakeKlipper
from pcbasm.kicad import load_board
from pcbasm.pasting.prepare import PasteCorrection
from pcbasm.pasting.session import PasteSession
from pcbasm.vision import Image, Offset
from tests.support.camera_model import make_camera_model
from tests.support.paths import TESTING_CONFIG_DIR, TESTING_DATA_DIR

LED_BLINKER = TESTING_DATA_DIR / "led_blinker" / "led_blinker.kicad_pcb"
PPM = 10.0
RESOLUTION = (640, 480)
FOCUS_Z = 12.0


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


def paste_session(
    camera: FakeCamera, klipper: FakeKlipper, machine: MachineConfig | None = None
) -> PasteSession:
    """計測済みの `PasteSession`（board は原点から (100, 50) へ載っている）.

    ``machine`` を省くと testing の machine.toml を使う。
    """
    board_frame = BoardFrame(
        board=load_board(LED_BLINKER),
        camera_model=make_camera_model(PPM, RESOLUTION, focus_z=FOCUS_Z),
        board_transform=Shift(100.0, 50.0),
    )
    return PasteSession.from_board_frame(
        board_frame,
        machine or load_machine(TESTING_CONFIG_DIR / "machine.toml"),
        klipper=klipper,
        stage=XYZStage(klipper),
        camera=camera,
    )


def full_board_correction() -> PasteCorrection:
    """基板全面を覆う成功領域 1 つ（変位なし）の補正."""
    area = shapely.box(-100.0, -100.0, 100.0, 100.0)
    return PasteCorrection(
        alignment=BoardCorrection(
            fits=(
                RegionFit(
                    region=AlignmentRegion(
                        index=0,
                        board_center=Point2d(0.0, 0.0),
                        anchor=Point2d(0.0, 0.0),
                        roi=(0, 0, 100, 100),
                        board_area=area,
                    ),
                    match=EdgeMatch(
                        offset=Offset(px=Point2d(0.0, 0.0)),
                        rms_distance_px=0.0,
                    ),
                    displacement=Point2d(0.0, 0.0),
                    increment=Point2d(0.0, 0.0),
                    passes=1,
                ),
            ),
            region_count=1,
        ),
        height_plane=Identity(),
    )


@attrs.define
class RecordingHooks:
    """手順の経過（ログ・進捗・表示した画像の枚数）を記録する hooks."""

    logs: list[str] = attrs.Factory(list)
    stages: list[str] = attrs.Factory(list)
    frames: int = 0

    def log(self, message: str) -> None:
        self.logs.append(message)

    def progress(self, stage: str, percent: float | None = None) -> None:
        self.stages.append(stage)

    def checkpoint(self) -> None:
        pass

    def frame(self, image: Image) -> None:
        self.frames += 1
