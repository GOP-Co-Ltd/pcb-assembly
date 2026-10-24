"""Board 座標から機械座標への変換を計測する."""

import logging
from collections.abc import Callable
from enum import Enum, auto

import numpy as np

from pcbasm.board import Outline
from pcbasm.config.reference_point import CornerOffsetsConfig, ReferencePointConfig
from pcbasm.gcode import GCode
from pcbasm.geometry import (
    Compose,
    Matrix2d,
    Point2d,
    Shift,
)
from pcbasm.hal import Klipper, Speed, XYZStage

logger = logging.getLogger(__name__)


class Corner(Enum):
    """ボードのコーナーを表す列挙型."""

    TOP_LEFT = auto()
    TOP_RIGHT = auto()
    BOTTOM_LEFT = auto()
    BOTTOM_RIGHT = auto()


def corner_offset(offsets: CornerOffsetsConfig, corner: Corner) -> Point2d:
    """指定コーナーのボード端から基準点マーカーへのオフセットを Point2d で返す."""
    match corner:
        case Corner.TOP_LEFT:
            offset = offsets.top_left
        case Corner.TOP_RIGHT:
            offset = offsets.top_right
        case Corner.BOTTOM_LEFT:
            offset = offsets.bottom_left
        case Corner.BOTTOM_RIGHT:
            offset = offsets.bottom_right

    return Point2d(x=offset[0], y=offset[1])


def _board_corner(outline: Outline, corner: Corner) -> Point2d:
    """指定コーナーの board 座標."""
    match corner:
        case Corner.TOP_LEFT:
            return Point2d(0.0, 0.0)
        case Corner.TOP_RIGHT:
            return Point2d(outline.width, 0.0)
        case Corner.BOTTOM_LEFT:
            return Point2d(0.0, outline.height)
        case Corner.BOTTOM_RIGHT:
            return Point2d(outline.width, outline.height)


class BoardTransformMeasurer:
    """Board 座標から機械座標への変換を計測するクラス.

    4 つの reference point の実測位置から affine 変換を最小二乗推定する。

    Example:
        from pcbasm.kicad import PcbFile

        pcb = PcbFile(pcb_file)
        measurer = BoardTransformMeasurer(
            adjust_reference=adjust_reference,
            klipper=klipper,
            stage=stage,
            outline=pcb.outline,
            reference_point=machine.reference_point,
            settle_sec=machine.settle.move_sec,
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
        reference_point: ReferencePointConfig,
        move_velocity_ratio: float = 0.9,
        *,
        settle_sec: float,
    ) -> None:
        """BoardTransformMeasurer を初期化する.

        Args:
            adjust_reference: 基準点マーカーを画像中心に合わせた機械座標を返す関数
                （通常は ``XYPositionAdjustor.adjust``）
            klipper: Klipper クライアント
            stage: XYZ ステージ
            outline: Board Outline（width/height の取得に使用）
            reference_point: 基準点設定
            move_velocity_ratio: 最大速度に対する移動速度の割合 (0.0-1.0)
            settle_sec: 移動後の静定待ち [sec]（``machine.settle.move_sec``）
        """
        self._adjust_reference = adjust_reference
        self._klipper = klipper
        self._stage = stage
        self._outline = outline
        self._ref_point = reference_point
        self._move_velocity_ratio = move_velocity_ratio
        self._settle_sec = settle_sec

    def measure(self) -> Compose:
        """4 点の最小二乗法で board→機械座標の変換を計測する.

        TOP_LEFT → TOP_RIGHT → BOTTOM_RIGHT → BOTTOM_LEFT の順に概略位置へ移動する。

        各コーナーでは ``adjust_reference()`` が返した位置を実測点にする。

        Returns:
            Board 座標→機械座標の Compose 変換 (Matrix2d → Shift)

        Raises:
            ValueError: 基準点配置の design matrix の rank が 3 未満の場合
            RuntimeError: ``adjust_reference`` の失敗をそのまま送出する
        """
        logger.info("Board変換の計測を開始")

        corners = (
            Corner.TOP_LEFT,
            Corner.TOP_RIGHT,
            Corner.BOTTOM_RIGHT,
            Corner.BOTTOM_LEFT,
        )
        logger.info("計測コーナー: TOP_LEFT, TOP_RIGHT, BOTTOM_RIGHT, BOTTOM_LEFT")
        move_velocity = self._stage.max_velocity * self._move_velocity_ratio
        machine_points = [
            self._measure_corner(corner, move_velocity) for corner in corners
        ]

        board_points = tuple(
            _board_corner(self._outline, corner)
            + corner_offset(self._ref_point.offsets, corner)
            for corner in corners
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
        logger.info(f"変換行列:\n{matrix_values}")
        logger.info(f"Board原点の機械座標: ({shift.x:.4f}, {shift.y:.4f})")

        # 変換を構成（Matrix2d → Shift）
        transform = Compose([matrix, shift])
        for corner, board_point, machine_point in zip(
            corners, board_points, machine_points, strict=True
        ):
            residual = transform.apply(board_point) - machine_point
            logger.info(
                f"残差 {corner.name}: ({residual.x:.4f}, {residual.y:.4f}) mm, "
                f"距離 {residual.norm:.4f} mm"
            )

        logger.info("Board変換の計測完了")
        return transform

    def _measure_corner(
        self,
        corner: Corner,
        move_velocity: float,
    ) -> Point2d:
        """指定コーナーへ移動し、位置補正した座標を返す."""
        ref_pos = self._approximate_position(corner)

        logger.info(f"=== {corner.name} Reference Pointへ移動 ===")
        logger.info(f"目標位置: ({ref_pos.x:.3f}, {ref_pos.y:.3f})")
        self._move_to(
            self._stage.move(
                x=ref_pos.x, y=ref_pos.y, speed=Speed.absolute(move_velocity)
            ),
        )

        logger.info(f"=== {corner.name} Reference Pointの位置補正 ===")
        pos = self._adjust_reference()
        logger.info(f"{corner.name}位置: ({pos.x:.4f}, {pos.y:.4f})")
        return pos

    def _approximate_position(self, corner: Corner) -> Point2d:
        """指定コーナーの基準点マーカーへ移動するための概略マシン座標を返す.

        ボード左上コーナーの概略位置 = 左上基準点の概略位置 - 左上のオフセット。
        各コーナーの概略位置 = ボード左上コーナーの概略位置 + コーナーの board 座標 + そのコーナーのオフセット。
        """
        ref = self._ref_point
        top_left = Point2d(ref.x, ref.y)
        if corner == Corner.TOP_LEFT:
            return top_left
        board_origin = top_left - corner_offset(ref.offsets, Corner.TOP_LEFT)
        return (
            board_origin
            + _board_corner(self._outline, corner)
            + corner_offset(ref.offsets, corner)
        )

    def _move_to(self, move_gcode: GCode) -> None:
        """指定座標に移動し、安定を待つ."""
        self._klipper.send_gcode(
            move_gcode + GCode.wait(self._settle_sec) + GCode.wait_for_done()
        )
