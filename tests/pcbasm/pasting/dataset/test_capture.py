"""DatasetCapturer（補正済み pad 位置での撮影と crop）の公開契約.

自前 HAL の fake（``FakeKlipper`` / ``FakeCamera``）に実 ``XYZStage`` / ``PasteSession`` /
``RegionAlignmentSession`` を組み合わせ、送信 G-code と返る crop で検証する。
"""

from datetime import UTC, datetime

import numpy as np
import pytest
import shapely

from pcbasm.config import Machine
from pcbasm.geometry import HeightPlane, Identity, Point2d, Point3d, Shift
from pcbasm.hal import XYZStage
from pcbasm.pasting.alignment import PasteCorrection
from pcbasm.pasting.dataset.capture import DatasetCapturer
from pcbasm.pasting.dataset.metadata import DatasetView
from pcbasm.pasting.session import PasteSession
from pcbasm.pcb import Pad, PcbFile
from pcbasm.posctrl import (
    AlignmentRegion,
    BoardAlignment,
    BoardCalibrationResult,
    EdgeMatch,
    RegionAlignment,
    RegionAlignmentSession,
)
from pcbasm.vision import Image, Offset
from pcbasm.vision.calibration import CalibrationResult
from tests.helpers import TESTING_CONFIG_DIR, TESTING_DATA_DIR, FakeCamera, FakeKlipper

LED_BLINKER = TESTING_DATA_DIR / "led_blinker" / "led_blinker.kicad_pcb"
PPM = 10.0
RESOLUTION = (640, 480)
FOCUS_Z = 12.0
BOARD_SHIFT = Point2d(100.0, 50.0)
DISPLACEMENT = Point2d(0.3, 0.1)


def _frame(width: int, height: int) -> Image:
    yy, xx = np.indices((height, width), dtype=np.uint16)
    return Image(np.dstack((xx % 256, yy % 256, (xx ^ yy) % 256)).astype(np.uint8))


def _calibration_result(camera: FakeCamera, klipper: FakeKlipper):
    return BoardCalibrationResult(
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
            calibrated_at=datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC),
            z_position=FOCUS_Z,
        ),
        offset_transform=Identity(),
        board_transform=Shift(BOARD_SHIFT.x, BOARD_SHIFT.y),
        pcb=PcbFile(LED_BLINKER),
    )


def _correction() -> PasteCorrection:
    area = shapely.box(-5.0, -5.0, 30.0, 30.0)
    center = area.centroid
    region = RegionAlignment(
        region=AlignmentRegion(
            index=0,
            board_center=Point2d(center.x, center.y),
            anchor=Point2d(center.x, center.y),
            roi=(0, 0, 100, 100),
            board_area=area,
        ),
        match=EdgeMatch(
            offset=Offset(px=Point2d(0.0, 0.0), pixel_per_mm=PPM),
            rms_distance_px=0.0,
        ),
        displacement=DISPLACEMENT,
        increment=Point2d(0.0, 0.0),
        passes=1,
    )
    points = [(0.0, 0.0), (3.0, 0.0), (0.0, 4.0), (3.0, 4.0), (1.0, 2.0), (2.0, 1.0)]
    height_plane = HeightPlane(tuple(Point3d(x, y, 0.0) for x, y in points))
    return PasteCorrection(BoardAlignment(results=(region,)), height_plane)


def _rect_center(rect: tuple[int, int, int, int]) -> tuple[float, float]:
    x0, y0, x1, y1 = rect
    return ((x0 + x1) / 2, (y0 + y1) / 2)


@pytest.fixture
def pad() -> Pad:
    return next(pad for pad in PcbFile(LED_BLINKER).pads if pad.designator == "D1")


def _capturer(
    camera: FakeCamera, klipper: FakeKlipper, frames: list[Image] | None = None
) -> DatasetCapturer:
    result = _calibration_result(camera, klipper)
    session = PasteSession.from_calibration(result)
    return DatasetCapturer(
        session,
        RegionAlignmentSession(result),
        _correction(),
        crop_margin_mm=1.0,
        mask_margin_mm=0.1,
        settle_time=0.0,
        frame_sink=None if frames is None else frames.append,
    )


class TestDatasetCapturer:
    def test_moves_camera_to_corrected_pad_center_at_focus_height(self, pad: Pad):
        klipper = FakeKlipper()
        capturer = _capturer(FakeCamera([_frame(*RESOLUTION)]), klipper)
        klipper.clear_sent()

        crop, error = capturer.capture(pad, DatasetView(number=0))

        assert error is None
        assert crop is not None
        move = klipper.g1_moves()[-1]
        assert move["x"] == pytest.approx(pad.center.x + BOARD_SHIFT.x + DISPLACEMENT.x)
        assert move["y"] == pytest.approx(pad.center.y + BOARD_SHIFT.y + DISPLACEMENT.y)
        assert move["z"] == pytest.approx(FOCUS_Z)

    def test_crop_is_centered_on_pad_and_mask_covers_pad(self, pad: Pad):
        frame = _frame(*RESOLUTION)
        crop, _ = _capturer(FakeCamera([frame]), FakeKlipper()).capture(
            pad, DatasetView(number=0)
        )

        assert crop is not None
        width, height = RESOLUTION
        center_x, center_y = _rect_center(crop.pixel_rect)
        assert center_x == pytest.approx(width / 2, abs=1.0)
        assert center_y == pytest.approx(height / 2, abs=1.0)
        min_x, min_y, max_x, max_y = pad.polygon.bounds
        assert crop.image.shape[1] == pytest.approx((max_x - min_x + 2.0) * PPM, abs=2)
        assert crop.image.shape[0] == pytest.approx((max_y - min_y + 2.0) * PPM, abs=2)
        x0, y0, x1, y1 = crop.pixel_rect
        assert np.array_equal(crop.image, frame.numpy()[y0:y1, x0:x1])
        assert crop.mask[crop.mask.shape[0] // 2, crop.mask.shape[1] // 2] == 255
        assert crop.mask[0, 0] == 0

    def test_view_offset_shifts_stage_and_crop_together(self, pad: Pad):
        klipper = FakeKlipper()
        capturer = _capturer(FakeCamera([_frame(*RESOLUTION)]), klipper)
        offset = Point2d(0.5, -1.0)

        centered, _ = capturer.capture(pad, DatasetView(number=0))
        klipper.clear_sent()
        shifted, _ = capturer.capture(
            pad, DatasetView(number=1, offset_x_mm=offset.x, offset_y_mm=offset.y)
        )

        assert centered is not None and shifted is not None
        move = klipper.g1_moves()[-1]
        assert move["x"] == pytest.approx(
            pad.center.x + BOARD_SHIFT.x + DISPLACEMENT.x + offset.x
        )
        assert move["y"] == pytest.approx(
            pad.center.y + BOARD_SHIFT.y + DISPLACEMENT.y + offset.y
        )
        # 投影公式 pixel = center + ppm * (stage - T_b(board)) より、ステージが +offset
        # 動くと pad は画像上で +offset*ppm ずれる
        base_x, base_y = _rect_center(centered.pixel_rect)
        moved_x, moved_y = _rect_center(shifted.pixel_rect)
        assert moved_x - base_x == pytest.approx(offset.x * PPM, abs=1.0)
        assert moved_y - base_y == pytest.approx(offset.y * PPM, abs=1.0)

    def test_sends_captured_frame_to_frame_sink(self, pad: Pad):
        frame = _frame(*RESOLUTION)
        frames: list[Image] = []
        capturer = _capturer(FakeCamera([frame]), FakeKlipper(), frames)

        capturer.capture(pad, DatasetView(number=0))

        assert frames == [frame]

    def test_reports_crop_outside_frame_without_raising(self, pad: Pad):
        capturer = _capturer(FakeCamera([_frame(40, 30)]), FakeKlipper())

        crop, error = capturer.capture(pad, DatasetView(number=0))

        assert crop is None
        assert error is not None
        assert "収まりません" in error
