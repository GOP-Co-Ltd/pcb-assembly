"""PointCapturer（board 座標の点での撮影と固定寸法 crop）の公開契約.

自前 HAL の fake（``FakeKlipper`` / ``FakeCamera``）に実 ``XYZStage`` /
``PasteSession`` を組み合わせ、送信 G-code と返る crop で検証する。位置合わせ補正を
渡さない場合は ``board_transform`` だけでカメラ位置と board→pixel affine が決まる。
"""

from datetime import UTC, datetime

import numpy as np
import pytest
import shapely

from pcbasm.config import Machine
from pcbasm.geometry import Identity, Point2d, Shift
from pcbasm.hal import XYZStage
from pcbasm.pasting.capture import PointCapturer
from pcbasm.pasting.session import PasteSession
from pcbasm.pcb import Copper, Layer, PcbFile
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
CENTER = Point2d(5.0, 4.0)
OFFSETS = (
    Point2d(0.0, 0.0),
    Point2d(1.0, 0.0),
    Point2d(0.0, 1.0),
    Point2d(-1.0, 0.0),
    Point2d(0.0, -1.0),
)


def _frame(width: int, height: int) -> Image:
    yy, xx = np.indices((height, width), dtype=np.uint16)
    return Image(np.dstack((xx % 256, yy % 256, (xx ^ yy) % 256)).astype(np.uint8))


def _session(
    camera: FakeCamera, klipper: FakeKlipper, machine: Machine | None = None
) -> PasteSession:
    result = BoardCalibrationResult(
        machine=machine or Machine(TESTING_CONFIG_DIR / "machine.toml"),
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
) -> PointCapturer:
    return PointCapturer(
        _session(camera, klipper),
        crop_size_px=CROP_SIZE_PX,
        frame_sink=None if frames is None else frames.append,
    )


def _rect_center(rect: tuple[int, int, int, int]) -> tuple[float, float]:
    x0, y0, x1, y1 = rect
    return ((x0 + x1) / 2, (y0 + y1) / 2)


class TestPointCapturer:
    """点 + offset への移動と、呼び出しをまたいで同寸法の crop."""

    def test_moves_camera_to_the_point_at_focus_height(self):
        klipper = FakeKlipper()
        capturer = _capturer(FakeCamera([_frame(*RESOLUTION)]), klipper)
        klipper.clear_sent()

        crop, error = capturer.capture(CENTER)

        assert error is None
        assert crop is not None
        move = klipper.g1_moves()[-1]
        assert move["x"] == pytest.approx(CENTER.x + BOARD_SHIFT.x)
        assert move["y"] == pytest.approx(CENTER.y + BOARD_SHIFT.y)
        assert move["z"] == pytest.approx(FOCUS_Z)

    def test_every_offset_is_reflected_in_the_sent_gcode(self):
        klipper = FakeKlipper()
        capturer = _capturer(FakeCamera([_frame(*RESOLUTION)]), klipper)
        targets: list[tuple[float, float]] = []

        for offset in OFFSETS:
            klipper.clear_sent()
            crop, error = capturer.capture(CENTER, offset=offset)
            assert error is None, error
            assert crop is not None
            move = klipper.g1_moves()[-1]
            targets.append((move["x"], move["y"]))

        assert targets == [
            pytest.approx(
                (
                    CENTER.x + BOARD_SHIFT.x + offset.x,
                    CENTER.y + BOARD_SHIFT.y + offset.y,
                )
            )
            for offset in OFFSETS
        ]

    def test_crop_pixel_size_is_the_same_for_every_offset_and_position(self):
        capturer = _capturer(FakeCamera([_frame(*RESOLUTION)]), FakeKlipper())
        centers = [
            Point2d(5.0, 4.0),
            Point2d(8.05, 4.0),
            Point2d(11.049, 7.951),
            Point2d(14.5, 10.5),
        ]

        sizes = set()
        for offset in OFFSETS:
            crop, error = capturer.capture(CENTER, offset=offset)
            assert error is None, error
            assert crop is not None
            x0, y0, x1, y1 = crop.pixel_rect
            sizes.add((x1 - x0, y1 - y0, *crop.image.shape))
        for center in centers:
            crop, error = capturer.capture(center)
            assert error is None, error
            assert crop is not None
            x0, y0, x1, y1 = crop.pixel_rect
            sizes.add((x1 - x0, y1 - y0, *crop.image.shape))

        assert sizes == {(CROP_SIZE_PX, CROP_SIZE_PX, CROP_SIZE_PX, CROP_SIZE_PX, 3)}

    def test_crop_is_centered_in_the_frame_when_no_offset_is_given(self):
        frame = _frame(*RESOLUTION)

        crop, error = _capturer(FakeCamera([frame]), FakeKlipper()).capture(CENTER)

        assert error is None
        assert crop is not None
        width, height = RESOLUTION
        center_x, center_y = _rect_center(crop.pixel_rect)
        assert center_x == pytest.approx(width / 2, abs=1.0)
        assert center_y == pytest.approx(height / 2, abs=1.0)
        x0, y0, x1, y1 = crop.pixel_rect
        assert np.array_equal(crop.image, frame.numpy()[y0:y1, x0:x1])

    def test_offset_shifts_the_crop_window_by_offset_times_scale(self):
        capturer = _capturer(FakeCamera([_frame(*RESOLUTION)]), FakeKlipper())
        offset = Point2d(0.5, -1.0)

        centered, _ = capturer.capture(CENTER)
        shifted, _ = capturer.capture(CENTER, offset=offset)

        assert centered is not None and shifted is not None
        # 投影公式 pixel = center + ppm * (stage - T_b(board)) より、ステージが
        # +offset 動くと点は画像上で +offset*ppm ずれる
        base_x, base_y = _rect_center(centered.pixel_rect)
        moved_x, moved_y = _rect_center(shifted.pixel_rect)
        assert moved_x - base_x == pytest.approx(offset.x * PPM, abs=1.0)
        assert moved_y - base_y == pytest.approx(offset.y * PPM, abs=1.0)

    def test_sends_captured_frame_to_frame_sink(self):
        frame = _frame(*RESOLUTION)
        frames: list[Image] = []
        capturer = _capturer(FakeCamera([frame]), FakeKlipper(), frames)

        capturer.capture(CENTER)

        assert frames == [frame]

    def test_reports_crop_outside_frame_without_raising(self):
        capturer = _capturer(FakeCamera([_frame(16, 12)]), FakeKlipper())

        crop, error = capturer.capture(CENTER)

        assert crop is None
        assert error is not None
        assert "収まりません" in error


class TestPointCapturerWithAlignmentCorrection:
    """位置合わせ補正を渡すと、塗布と同じ補正後の位置を撮る."""

    def test_moves_the_camera_by_the_correction(self):
        klipper = FakeKlipper()
        capturer = _capturer(FakeCamera([_frame(*RESOLUTION)]), klipper)
        klipper.clear_sent()

        crop, error = capturer.capture(CENTER, correction=Shift(0.3, -0.2))

        assert error is None
        assert crop is not None
        move = klipper.g1_moves()[-1]
        assert move["x"] == pytest.approx(CENTER.x + BOARD_SHIFT.x + 0.3)
        assert move["y"] == pytest.approx(CENTER.y + BOARD_SHIFT.y - 0.2)

    def test_keeps_the_corrected_point_at_the_frame_center(self):
        """補正でステージも投影も同じだけ動くので、crop は画面中央のまま."""
        capturer = _capturer(FakeCamera([_frame(*RESOLUTION)]), FakeKlipper())

        centered, _ = capturer.capture(CENTER)
        corrected, _ = capturer.capture(CENTER, correction=Shift(0.3, -0.2))

        assert centered is not None and corrected is not None
        assert _rect_center(corrected.pixel_rect) == pytest.approx(
            _rect_center(centered.pixel_rect), abs=1.0
        )


def _machine_with_settle(tmp_path, *, move_sec: float, probe_sec: float) -> Machine:
    """検証用 machine.toml の末尾に `[settle]` を足した Machine を作る."""
    path = tmp_path / "machine.toml"
    source = (TESTING_CONFIG_DIR / "machine.toml").read_text(encoding="utf-8")
    path.write_text(
        f"{source}\n[settle]\nmove_sec = {move_sec}\nprobe_sec = {probe_sec}\n",
        encoding="utf-8",
    )
    return Machine(path)


class TestSettleWiring:
    """`[settle]` の値が、撮影前とプローブ後の dwell として実際の G-code に届く.

    移動後の静定（``move_sec``）とプローブ後の待ち（``probe_sec``）は別の設定値で、
    配線を取り違えても引数の型は合ってしまう。送信 G-code の dwell で区別を固定する。
    """

    @pytest.fixture
    def machine(self, tmp_path) -> Machine:
        return _machine_with_settle(tmp_path, move_sec=0.8, probe_sec=0.3)

    def test_capture_dwells_for_the_move_settle(self, machine: Machine):
        klipper = FakeKlipper()
        session = _session(FakeCamera([_frame(*RESOLUTION)]), klipper, machine)
        capturer = PointCapturer(session, crop_size_px=CROP_SIZE_PX)

        capturer.capture(CENTER)

        assert "G4 P800" in klipper.sent_lines

    def test_probe_dwells_for_the_probe_settle(self, machine: Machine):
        klipper = FakeKlipper()
        session = _session(FakeCamera([_frame(*RESOLUTION)]), klipper, machine)

        session.probe_executor.probe()

        assert "G4 P300" in klipper.sent_lines

    def test_height_measurement_dwells_for_the_move_settle(self, machine: Machine):
        """高さ計測の点間移動は、プローブ後の待ちではなく移動の静定を使う."""
        klipper = FakeKlipper()
        session = _session(FakeCamera([_frame(*RESOLUTION)]), klipper, machine)
        copper = Copper(
            layer=Layer.TOP,
            polygon=shapely.Polygon(
                [(0.0, 0.0), (40.0, 0.0), (40.0, 40.0), (0.0, 40.0)]
            ),
        )

        session.height_measurer.measure([copper], Identity(), copper.polygon)

        # 移動の後は 800 ms、PROBE の後は 300 ms。入れ替わっていたら落ちる
        lines = klipper.sent_lines
        probe_index = lines.index("PROBE")
        assert "G4 P800" in lines[:probe_index]
        assert next(line for line in lines[probe_index:] if line.startswith("G4")) == (
            "G4 P300"
        )
