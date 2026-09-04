"""補正済み pad 位置へ camera を動かし、pad 周辺の crop / mask を取得する."""

from __future__ import annotations

from pcbasm.gcode import GCode
from pcbasm.geometry import Point2d
from pcbasm.pasting.alignment import PasteCorrection
from pcbasm.pasting.dataset.metadata import DatasetView
from pcbasm.pasting.session import PasteSession
from pcbasm.pcb import Pad
from pcbasm.posctrl import RegionAlignmentSession
from pcbasm.vision.crop import PolygonCrop, crop_polygon
from pcbasm.vision.image import FrameSink


class DatasetCapturer:
    """Dataset 収集用に pad 1 枚・view 1 つの撮影と crop を行う.

    ``session.camera_target`` でカメラ中心へ補正済み pad 中心を置き、settle 後に撮影し、
    領域補正を含めた board→pixel affine で :func:`crop_polygon` する。
    """

    def __init__(
        self,
        session: PasteSession,
        alignment_session: RegionAlignmentSession,
        correction: PasteCorrection,
        *,
        crop_margin_mm: float,
        mask_margin_mm: float,
        settle_time: float = 0.5,
        frame_sink: FrameSink | None = None,
    ) -> None:
        self._session = session
        self._alignment_session = alignment_session
        self._correction = correction
        self._crop_margin_mm = crop_margin_mm
        self._mask_margin_mm = mask_margin_mm
        self._settle_time = settle_time
        self._frame_sink = frame_sink

    def capture(
        self, pad: Pad, view: DatasetView
    ) -> tuple[PolygonCrop | None, str | None]:
        """補正済み pad 位置（+ view offset）へ移動して撮影し、crop / mask を返す."""
        session = self._session
        shift = self._correction.alignment.correction_for(
            pad.center, designator=pad.designator
        )
        target = session.camera_target(
            pad,
            self._correction,
            offset=Point2d(view.offset_x_mm, view.offset_y_mm),
        )
        session.klipper.send_gcode(
            session.stage.move(x=target.x, y=target.y, z=session.calibration.z_position)
            + GCode.wait(self._settle_time)
            + GCode.wait_for_done()
        )
        image = session.camera.capture()
        if self._frame_sink is not None:
            self._frame_sink(image)
        projector = self._alignment_session.projector.with_correction(shift)
        matrix, translation = projector.board_to_pixel_affine(target)
        return crop_polygon(
            image,
            pad.polygon,
            matrix,
            translation,
            margin_mm=self._crop_margin_mm,
            mask_margin_mm=self._mask_margin_mm,
        )
