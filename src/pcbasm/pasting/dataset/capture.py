"""セル中心へ camera を動かし、固定ピクセル寸法の crop を取得する."""

from __future__ import annotations

from pcbasm.gcode import GCode
from pcbasm.geometry import Point2d
from pcbasm.pasting.dataset.metadata import DatasetView
from pcbasm.pasting.dataset.plan import DotTarget
from pcbasm.pasting.session import PasteSession
from pcbasm.posctrl import CopperProjector
from pcbasm.vision.crop import RectCrop, crop_centered
from pcbasm.vision.image import FrameSink


class DatasetCapturer:
    """Dataset 収集用にセル 1 個・view 1 つの撮影と crop を行う.

    銅板には照合対象の銅箔島パターンが無いので領域照合は使わず、``board_transform``
    だけでカメラ位置と board→pixel affine を決める。crop は
    :func:`~pcbasm.vision.crop.crop_centered` でセル中心の周りを固定寸法で切り出すため、
    全セル・全 view で同一ピクセル寸法になる。
    """

    def __init__(
        self,
        session: PasteSession,
        *,
        crop_size_px: int,
        settle_time: float = 0.5,
        frame_sink: FrameSink | None = None,
    ) -> None:
        """Board 計測済みセッションと、収集開始時に 1 回決めた crop 寸法で組む."""
        self._session = session
        self._crop_size_px = crop_size_px
        self._settle_time = settle_time
        self._frame_sink = frame_sink
        self._projector = CopperProjector.from_calibration(session.calibration_result)

    def capture(
        self, target: DotTarget, view: DatasetView
    ) -> tuple[RectCrop | None, str | None]:
        """セル中心（+ view offset）へ移動して撮影し、固定寸法の crop を返す."""
        session = self._session
        stage_xy = session.camera_point_target(
            target.center, offset=Point2d(view.offset_x_mm, view.offset_y_mm)
        )
        session.klipper.send_gcode(
            session.stage.move(
                x=stage_xy.x, y=stage_xy.y, z=session.calibration.z_position
            )
            + GCode.wait(self._settle_time)
            + GCode.wait_for_done()
        )
        image = session.camera.capture()
        if self._frame_sink is not None:
            self._frame_sink(image)
        matrix, translation = self._projector.board_to_pixel_affine(stage_xy)
        return crop_centered(
            image,
            target.center,
            matrix,
            translation,
            pixel_size=self._crop_size_px,
        )
