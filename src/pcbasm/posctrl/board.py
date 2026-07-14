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


def _select_corners(ref_point: ReferencePoint) -> tuple[Corner, Corner]:
    """計測に使用する2つのコーナーを選択する.

    TOP_LEFT以外の利用可能なコーナーから2つを選択する。 優先順位: (TR, BL) → (TR, BR) → (BL, BR)
    """
    offsets = ref_point.offsets
    has_tr = offsets.has_corner(Corner.TOP_RIGHT)
    has_bl = offsets.has_corner(Corner.BOTTOM_LEFT)

    if has_tr and has_bl:
        return Corner.TOP_RIGHT, Corner.BOTTOM_LEFT
    if has_tr:
        return Corner.TOP_RIGHT, Corner.BOTTOM_RIGHT
    return Corner.BOTTOM_LEFT, Corner.BOTTOM_RIGHT


class BoardTransformMeasurer:
    """Board座標から機械座標への変換を計測するクラス.

    3点法を用いて、reference pointの実測位置から
    2x2変換行列と平行移動を計算する。

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
        """3点法でboard→機械座標の変換を計測する.

        TOP_LEFTと他2コーナーの実測位置から2x2変換行列を求める。

        Returns:
            Board座標→機械座標のCompose変換 (Matrix2d → Shift)
        """
        self._logger.info("Board変換の計測を開始")

        corner_a, corner_b = _select_corners(self._ref_point)
        self._logger.info(f"計測コーナー: TOP_LEFT, {corner_a.name}, {corner_b.name}")

        offset_tl = self._ref_point.offsets.get(Corner.TOP_LEFT)
        move_velocity = self._stage.max_velocity * self._move_velocity_ratio

        # --- TOP_LEFT ---
        pos_tl = self._measure_corner(Corner.TOP_LEFT, move_velocity)

        # --- Corner A ---
        pos_a = self._measure_corner(corner_a, move_velocity)

        # --- Corner B ---
        pos_b = self._measure_corner(corner_b, move_velocity)

        # ボード空間でのTL→A, TL→Bベクトル（理論値）
        ref_pos_tl = self._get_reference_position(Corner.TOP_LEFT)
        board_vec_a = self._get_reference_position(corner_a) - ref_pos_tl
        board_vec_b = self._get_reference_position(corner_b) - ref_pos_tl

        # 機械空間での実測ベクトル
        mach_vec_a = pos_a - pos_tl
        mach_vec_b = pos_b - pos_tl

        self._logger.info(
            f"ボード空間ベクトルA: ({board_vec_a.x:.3f}, {board_vec_a.y:.3f})"
        )
        self._logger.info(
            f"ボード空間ベクトルB: ({board_vec_b.x:.3f}, {board_vec_b.y:.3f})"
        )
        self._logger.info(
            f"機械空間ベクトルA: ({mach_vec_a.x:.4f}, {mach_vec_a.y:.4f})"
        )
        self._logger.info(
            f"機械空間ベクトルB: ({mach_vec_b.x:.4f}, {mach_vec_b.y:.4f})"
        )

        # 2x2変換行列を計算: T = M @ B^(-1)
        b_mat = np.array(
            [[board_vec_a.x, board_vec_b.x], [board_vec_a.y, board_vec_b.y]]
        )
        m_mat = np.array([[mach_vec_a.x, mach_vec_b.x], [mach_vec_a.y, mach_vec_b.y]])
        t_mat = m_mat @ np.linalg.inv(b_mat)
        matrix = Matrix2d(t_mat)
        self._logger.info(f"変換行列:\n{t_mat}")

        # Board原点の機械座標を計算
        board_origin = pos_tl - matrix.apply(offset_tl)
        self._logger.info(
            f"Board左上コーナーの機械座標: ({board_origin.x:.4f}, {board_origin.y:.4f})"
        )

        # 変換を構成（Matrix2d → Shift）
        transform = Compose([matrix, Shift.from_point(board_origin)])

        self._logger.info("Board変換の計測完了")
        return transform

    def _get_reference_position(self, corner: Corner) -> Point2d:
        """指定コーナーの理論的な基準点位置を返す."""
        return self._ref_point.get_reference_position(
            corner,
            board_width=self._outline.width,
            board_height=self._outline.height,
        )

    def _measure_corner(
        self,
        corner: Corner,
        move_velocity: float,
    ) -> Point2d:
        """指定コーナーへ移動し、位置補正した座標を返す."""
        ref_pos = self._get_reference_position(corner)

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
