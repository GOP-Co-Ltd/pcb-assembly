"""Board座標から機械座標への変換を計測する."""

import logging
from collections.abc import Callable

import numpy as np

from pcbasm import gcode
from pcbasm.config import Corner, ReferencePoint
from pcbasm.geometry import (
    Compose,
    Matrix2d,
    Point2d,
    Shift,
)
from pcbasm.hal import Klipper, Speed, XYZStage
from pcbasm.pcb import Outline
from pcbasm.utils import get_class_module_path


class BoardTransformMeasurer:
    """Board座標から機械座標への変換を計測するクラス.

    4つのreference pointの実測位置からaffine変換を最小二乗推定する。

    Example:
        from pcbasm.pcb import PcbFile

        pcb = PcbFile(pcb_file)
        measurer = BoardTransformMeasurer(
            adjust_reference=adjust_reference,
            klipper=klipper,
            stage=stage,
            outline=pcb.outline,
            reference_point=machine.reference_point,
        )
        transform = measurer.measure()
        machine_pos = transform.apply(board_pos)
    """

    def __init__(
        self,
        adjust_reference: Callable[[], Point2d],
        klipper: Klipper,
        stage: XYZStage,
        outline: Outline,
        reference_point: ReferencePoint,
        move_velocity_ratio: float = 0.9,
        settle_time: float = 0.5,
    ) -> None:
        """BoardTransformMeasurerを初期化する.

        Args:
            adjust_reference: 補正済みオフセットを返す関数
            klipper: Klipperクライアント
            stage: XYZステージ
            outline: Board Outline（width/heightの取得に使用）
            reference_point: 基準点設定
            move_velocity_ratio: 最大速度に対する移動速度の割合 (0.0-1.0)
            settle_time: 移動後の安定待機時間（秒）
        """
        self._adjust_reference = adjust_reference
        self._klipper = klipper
        self._stage = stage
        self._outline = outline
        self._ref_point = reference_point
        self._move_velocity_ratio = move_velocity_ratio
        self._settle_time = settle_time

        self._logger = logging.getLogger(get_class_module_path(self.__class__))

    def measure(self) -> Compose:
        """4点の最小二乗法でboard→機械座標の変換を計測する.

        Returns:
            Board座標→機械座標のCompose変換 (Matrix2d → Shift)

        Raises:
            ValueError: 基準点配置のdesign matrixのrankが3未満の場合
        """
        self._logger.info("Board変換の計測を開始")

        corners = (
            Corner.TOP_LEFT,
            Corner.TOP_RIGHT,
            Corner.BOTTOM_RIGHT,
            Corner.BOTTOM_LEFT,
        )
        self._logger.info(
            "計測コーナー: TOP_LEFT, TOP_RIGHT, BOTTOM_RIGHT, BOTTOM_LEFT"
        )
        move_velocity = self._stage.max_velocity * self._move_velocity_ratio
        machine_points = [
            self._measure_corner(corner, move_velocity) for corner in corners
        ]

        offsets = self._ref_point.offsets
        board_points = (
            offsets.get(Corner.TOP_LEFT),
            Point2d(self._outline.width, 0.0) + offsets.get(Corner.TOP_RIGHT),
            Point2d(self._outline.width, self._outline.height)
            + offsets.get(Corner.BOTTOM_RIGHT),
            Point2d(0.0, self._outline.height) + offsets.get(Corner.BOTTOM_LEFT),
        )
        design = np.array(
            [[point.x, point.y, 1.0] for point in board_points],
        )
        measured = np.array([[point.x, point.y] for point in machine_points])
        params, _, rank, _ = np.linalg.lstsq(design, measured, rcond=None)
        if rank < 3:
            raise ValueError(
                f"基準点配置のdesign matrixのrankが不足しています: rank={rank}"
            )

        matrix_values = params[:2].T
        translation = params[2]
        matrix = Matrix2d(matrix_values)
        shift = Shift(x=float(translation[0]), y=float(translation[1]))
        self._logger.info(f"変換行列:\n{matrix_values}")
        self._logger.info(f"Board原点の機械座標: ({shift.x:.4f}, {shift.y:.4f})")

        # 変換を構成（Matrix2d → Shift）
        transform = Compose([matrix, shift])
        for corner, board_point, machine_point in zip(
            corners, board_points, machine_points, strict=True
        ):
            residual = transform.apply(board_point) - machine_point
            self._logger.info(
                f"残差 {corner.name}: ({residual.x:.4f}, {residual.y:.4f}) mm, "
                f"距離 {residual.norm:.4f} mm"
            )

        self._logger.info("Board変換の計測完了")
        return transform

    def _measure_corner(
        self,
        corner: Corner,
        move_velocity: float,
    ) -> Point2d:
        """指定コーナーへ移動し、位置補正した座標を返す."""
        ref_pos = self._ref_point.get_reference_position(
            corner,
            board_width=self._outline.width,
            board_height=self._outline.height,
        )

        self._logger.info(f"=== {corner.name} Reference Pointへ移動 ===")
        self._logger.info(f"目標位置: ({ref_pos.x:.3f}, {ref_pos.y:.3f})")
        self._move_to(
            self._stage.move(
                x=ref_pos.x, y=ref_pos.y, speed=Speed.absolute(move_velocity)
            ),
        )

        self._logger.info(f"=== {corner.name} Reference Pointの位置補正 ===")
        pos = self._adjust_reference()
        self._logger.info(f"{corner.name}位置: ({pos.x:.4f}, {pos.y:.4f})")
        return pos

    def _move_to(self, move_gcode: gcode.GCode) -> None:
        """指定座標に移動し、安定を待つ."""
        self._klipper.send_gcode(
            move_gcode + gcode.wait(self._settle_time) + gcode.wait_for_done()
        )
