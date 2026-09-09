"""DatasetCapturer（セル中心での撮影と固定寸法 crop）の公開契約.

自前 HAL の fake（``FakeKlipper`` / ``FakeCamera``）に実 ``XYZStage`` /
``PasteSession`` を組み合わせ、送信 G-code と返る crop で検証する。銅板には照合
対象の銅箔島パターンが無いので領域照合は挟まず、``board_transform`` だけで
カメラ位置と board→pixel affine が決まる。
"""

from datetime import UTC, datetime

import numpy as np
import pytest

from pcbasm.config import Machine
from pcbasm.geometry import Identity, Point2d, Shift
from pcbasm.geometry.packing import Rect
from pcbasm.hal import XYZStage
from pcbasm.pasting.dataset.capture import DatasetCapturer
from pcbasm.pasting.dataset.metadata import DatasetView
from pcbasm.pasting.dataset.plan import DotBlank, DotCell, plan_views
from pcbasm.pasting.session import PasteSession
from pcbasm.pcb import PcbFile
from pcbasm.posctrl import BoardCalibrationResult
from pcbasm.vision import Image
from pcbasm.vision.calibration import CalibrationResult
from tests.helpers import TESTING_CONFIG_DIR, TESTING_DATA_DIR, FakeCamera, FakeKlipper

LED_BLINKER = TESTING_DATA_DIR / "led_blinker" / "led_blinker.kicad_pcb"
PPM = 10.0
RESOLUTION = (640, 480)
FOCUS_Z = 12.0
BOARD_SHIFT = Point2d(100.0, 50.0)
CROP_SIZE_PX = 21
VIEW_RADIUS_MM = 1.0


def _frame(width: int, height: int) -> Image:
    yy, xx = np.indices((height, width), dtype=np.uint16)
    return Image(np.dstack((xx % 256, yy % 256, (xx ^ yy) % 256)).astype(np.uint8))


def _cell(index: int, center: Point2d) -> DotCell:
    return DotCell(
        index=index,
        rect=Rect(center.x - 1.0, center.y - 1.0, 2.0, 2.0),
        center=center,
        commanded_volume_ul=0.125,
        volume_index=2,
        order=index,
    )


def _blank(index: int, center: Point2d) -> DotBlank:
    return DotBlank(
        index=index,
        rect=Rect(center.x - 1.0, center.y - 1.0, 2.0, 2.0),
        center=center,
    )


def _session(camera: FakeCamera, klipper: FakeKlipper) -> PasteSession:
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
            calibrated_at=datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC),
            z_position=FOCUS_Z,
        ),
        offset_transform=Identity(),
        board_transform=Shift(BOARD_SHIFT.x, BOARD_SHIFT.y),
        pcb=PcbFile(LED_BLINKER),
    )
    return PasteSession.from_calibration(result)


def _capturer(
    camera: FakeCamera, klipper: FakeKlipper, frames: list[Image] | None = None
) -> DatasetCapturer:
    return DatasetCapturer(
        _session(camera, klipper),
        crop_size_px=CROP_SIZE_PX,
        settle_time=0.0,
        frame_sink=None if frames is None else frames.append,
    )


def _rect_center(rect: tuple[int, int, int, int]) -> tuple[float, float]:
    x0, y0, x1, y1 = rect
    return ((x0 + x1) / 2, (y0 + y1) / 2)


@pytest.fixture
def views() -> tuple[DatasetView, ...]:
    planned, error = plan_views(4, VIEW_RADIUS_MM)

    assert error is None
    assert planned is not None
    return planned


class TestDatasetCapturer:
    """セル中心 + view offset への移動と、全 view 同寸法の crop."""

    def test_moves_camera_to_the_cell_center_at_focus_height(self):
        klipper = FakeKlipper()
        capturer = _capturer(FakeCamera([_frame(*RESOLUTION)]), klipper)
        cell = _cell(1, Point2d(5.0, 4.0))
        klipper.clear_sent()

        crop, error = capturer.capture(cell, DatasetView(number=0))

        assert error is None
        assert crop is not None
        move = klipper.g1_moves()[-1]
        assert move["x"] == pytest.approx(cell.center.x + BOARD_SHIFT.x)
        assert move["y"] == pytest.approx(cell.center.y + BOARD_SHIFT.y)
        assert move["z"] == pytest.approx(FOCUS_Z)

    def test_every_view_offset_is_reflected_in_the_sent_gcode(
        self, views: tuple[DatasetView, ...]
    ):
        klipper = FakeKlipper()
        capturer = _capturer(FakeCamera([_frame(*RESOLUTION)]), klipper)
        cell = _cell(1, Point2d(5.0, 4.0))
        targets: list[tuple[float, float]] = []

        for view in views:
            klipper.clear_sent()
            crop, error = capturer.capture(cell, view)
            assert error is None, error
            assert crop is not None
            move = klipper.g1_moves()[-1]
            targets.append((move["x"], move["y"]))

        assert targets == [
            pytest.approx(
                (
                    cell.center.x + BOARD_SHIFT.x + view.offset_x_mm,
                    cell.center.y + BOARD_SHIFT.y + view.offset_y_mm,
                )
            )
            for view in views
        ]

    def test_all_views_return_crops_of_the_same_pixel_size(
        self, views: tuple[DatasetView, ...]
    ):
        capturer = _capturer(FakeCamera([_frame(*RESOLUTION)]), FakeKlipper())
        cell = _cell(1, Point2d(5.0, 4.0))

        sizes = set()
        for view in views:
            crop, error = capturer.capture(cell, view)
            assert error is None, error
            assert crop is not None
            x0, y0, x1, y1 = crop.pixel_rect
            sizes.add((x1 - x0, y1 - y0, *crop.image.shape))

        assert sizes == {(CROP_SIZE_PX, CROP_SIZE_PX, CROP_SIZE_PX, CROP_SIZE_PX, 3)}

    def test_cells_at_different_positions_share_the_same_pixel_size(self):
        capturer = _capturer(FakeCamera([_frame(*RESOLUTION)]), FakeKlipper())
        centers = [
            Point2d(5.0, 4.0),
            Point2d(8.05, 4.0),
            Point2d(11.049, 7.951),
            Point2d(14.5, 10.5),
        ]

        shapes = set()
        for index, center in enumerate(centers, start=1):
            crop, error = capturer.capture(_cell(index, center), DatasetView(number=0))
            assert error is None, error
            assert crop is not None
            shapes.add(crop.image.shape)

        assert shapes == {(CROP_SIZE_PX, CROP_SIZE_PX, 3)}

    def test_crop_is_centered_in_the_frame_when_the_view_is_centered(self):
        frame = _frame(*RESOLUTION)
        crop, error = _capturer(FakeCamera([frame]), FakeKlipper()).capture(
            _cell(1, Point2d(5.0, 4.0)), DatasetView(number=0)
        )

        assert error is None
        assert crop is not None
        width, height = RESOLUTION
        center_x, center_y = _rect_center(crop.pixel_rect)
        assert center_x == pytest.approx(width / 2, abs=1.0)
        assert center_y == pytest.approx(height / 2, abs=1.0)
        x0, y0, x1, y1 = crop.pixel_rect
        assert np.array_equal(crop.image, frame.numpy()[y0:y1, x0:x1])

    def test_view_offset_shifts_the_crop_window_by_offset_times_scale(self):
        capturer = _capturer(FakeCamera([_frame(*RESOLUTION)]), FakeKlipper())
        cell = _cell(1, Point2d(5.0, 4.0))
        offset = Point2d(0.5, -1.0)

        centered, _ = capturer.capture(cell, DatasetView(number=0))
        shifted, _ = capturer.capture(
            cell,
            DatasetView(number=1, offset_x_mm=offset.x, offset_y_mm=offset.y),
        )

        assert centered is not None and shifted is not None
        # 投影公式 pixel = center + ppm * (stage - T_b(board)) より、ステージが
        # +offset 動くとセル中心は画像上で +offset*ppm ずれる
        base_x, base_y = _rect_center(centered.pixel_rect)
        moved_x, moved_y = _rect_center(shifted.pixel_rect)
        assert moved_x - base_x == pytest.approx(offset.x * PPM, abs=1.0)
        assert moved_y - base_y == pytest.approx(offset.y * PPM, abs=1.0)

    def test_blank_targets_are_captured_the_same_way(self):
        klipper = FakeKlipper()
        capturer = _capturer(FakeCamera([_frame(*RESOLUTION)]), klipper)
        blank = _blank(1, Point2d(5.0, 4.0))
        klipper.clear_sent()

        crop, error = capturer.capture(blank, DatasetView(number=0))

        assert error is None
        assert crop is not None
        assert crop.image.shape == (CROP_SIZE_PX, CROP_SIZE_PX, 3)
        move = klipper.g1_moves()[-1]
        assert move["x"] == pytest.approx(blank.center.x + BOARD_SHIFT.x)
        assert move["y"] == pytest.approx(blank.center.y + BOARD_SHIFT.y)

    def test_sends_captured_frame_to_frame_sink(self):
        frame = _frame(*RESOLUTION)
        frames: list[Image] = []
        capturer = _capturer(FakeCamera([frame]), FakeKlipper(), frames)

        capturer.capture(_cell(1, Point2d(5.0, 4.0)), DatasetView(number=0))

        assert frames == [frame]

    def test_reports_crop_outside_frame_without_raising(self):
        capturer = _capturer(FakeCamera([_frame(16, 12)]), FakeKlipper())

        crop, error = capturer.capture(_cell(1, Point2d(5.0, 4.0)), DatasetView(0))

        assert crop is None
        assert error is not None
        assert "収まりません" in error
