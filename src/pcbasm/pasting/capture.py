"""Board 座標の点へ camera を動かし、固定ピクセル寸法の crop を取得する."""

from __future__ import annotations

from pcbasm.gcode import GCode
from pcbasm.geometry import Point2d, Transform
from pcbasm.pasting.session import PasteSession
from pcbasm.posctrl import CopperProjector
from pcbasm.vision.crop import RectCrop, crop_centered
from pcbasm.vision.image import FrameSink

_NO_OFFSET = Point2d(0.0, 0.0)


class PointCapturer:
    """Board 座標の 1 点を撮影し、その周りを固定寸法で切り出す.

    dataset 収集（銅板のセル中心）と運転時キャリブレーション（基板上の測定点）で
    同じ経路を使う。
    crop は :func:`~pcbasm.vision.crop.crop_centered` で点の周りを固定寸法で切り出す
    ため、呼び出しをまたいで同一ピクセル寸法になる。

    銅板には照合対象の銅箔島パターンが無いので領域照合は使わず、``board_transform``
    だけでカメラ位置と board→pixel affine を決める。
    基板の位置合わせ補正がある場合は撮影ごとに ``correction`` で渡す。
    """

    def __init__(
        self,
        session: PasteSession,
        *,
        crop_size_px: int,
        settle_time: float = 0.5,
        frame_sink: FrameSink | None = None,
    ) -> None:
        """Board 計測済みセッションと、開始時に 1 回決めた crop 寸法で組む."""
        self._session = session
        self._crop_size_px = crop_size_px
        self._settle_time = settle_time
        self._frame_sink = frame_sink
        self._projector = CopperProjector.from_calibration(session.calibration_result)

    def capture(
        self,
        center: Point2d,
        *,
        offset: Point2d = _NO_OFFSET,
        correction: Transform | None = None,
    ) -> tuple[RectCrop | None, str | None]:
        """点（+ offset）へ移動して撮影し、固定寸法の crop を返す.

        Args:
            center: 撮影する board 座標の点
            offset: カメラ中心をずらす量 [mm]（複数視点の撮影用）
            correction: その点に効く機械座標の位置合わせ補正（無ければ ``None``）
        """
        session = self._session
        stage_xy = session.camera_point_target(
            center, offset=offset, correction=correction
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
        projector = (
            self._projector
            if correction is None
            else self._projector.with_correction(correction)
        )
        matrix, translation = projector.board_to_pixel_affine(stage_xy)
        return crop_centered(
            image,
            center,
            matrix,
            translation,
            pixel_size=self._crop_size_px,
        )
