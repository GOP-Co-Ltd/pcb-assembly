"""位置を補正する."""

import logging
from collections.abc import Callable

from pcb_assembly import gcode
from pcb_assembly.geometry import Move, Point2d
from pcb_assembly.hal import Klipper, XYZStage
from pcb_assembly.utils import get_class_module_path


class XYPositionAdjustor:
    """XY位置を反復的に補正するクラス.

    大まかに位置合わせした状態から、観測されたオフセットを元に
    許容誤差内に収束するまで位置を微調整する。

    Example:
        adjustor = PositionAdjustor(tolerance=0.01)
        final_pos = adjustor.adjust(observe_offset, klipper, stage)
    """

    def __init__(
        self,
        tolerance: float = 0.01,
        max_iterations: int = 10,
        move_velocity_ratio: float = 0.5,
        settle_time: float = 0.5,
    ) -> None:
        """PositionAdjustorを初期化する.

        Args:
            tolerance: 許容誤差 (mm)
            max_iterations: 最大反復回数
            move_velocity_ratio: 最大速度に対する移動速度の割合 (0.0-1.0)
            settle_time: 移動後の安定待機時間（秒）
        """
        self._tolerance = tolerance
        self._max_iterations = max_iterations
        self._move_velocity_ratio = move_velocity_ratio
        self._settle_time = settle_time

        self._logger = logging.getLogger(get_class_module_path(self.__class__))

    def adjust(
        self,
        observe_offset: Callable[[], Point2d],
        klipper: Klipper,
        stage: XYZStage,
    ) -> Point2d:
        """位置を反復的に補正する.

        Args:
            observe_offset: オフセットを検出して返す関数
            klipper: Klipperクライアント
            stage: XYZステージ

        Returns:
            補正後の最終XY位置

        Raises:
            RuntimeError: 最大反復回数内に収束しなかった場合
        """
        self._logger.info("位置補正を開始")

        move_velocity = stage.max_velocity * self._move_velocity_ratio
        offset = Point2d(x=0.0, y=0.0)
        for iteration in range(self._max_iterations):
            offset = observe_offset()
            self._logger.info(
                f"試行 {iteration + 1}/{self._max_iterations}: "
                f"オフセット ({offset.x:.4f}, {offset.y:.4f}) mm, "
                f"距離 {offset.norm:.4f} mm"
            )

            if offset.norm < self._tolerance:
                pos = stage.get_position()
                self._logger.info(
                    f"許容誤差 {self._tolerance} mm 以内に収束: "
                    f"最終位置 ({pos.x:.4f}, {pos.y:.4f})"
                )
                return pos.to2d()

            # オフセット分だけ移動
            pos = stage.get_position()
            target = pos.to2d() - offset
            self._logger.debug(
                f"移動: ({pos.x:.4f}, {pos.y:.4f}) -> "
                f"({target.x:.4f}, {target.y:.4f})"
            )

            self._move_to(
                klipper,
                stage.to_gcode(Move.from_point(target, v=move_velocity)),
            )

        raise RuntimeError(
            f"{self._max_iterations}回の試行で収束しませんでした "
            f"(最終オフセット: {offset.norm:.4f} mm)"
        )

    def _move_to(
        self,
        klipper: Klipper,
        move_gcode: gcode.GCode,
    ) -> None:
        """指定座標に移動し、安定を待つ."""
        klipper.send_gcode(
            move_gcode + gcode.wait(self._settle_time) + gcode.wait_for_done()
        )
