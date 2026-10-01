"""位置を補正する."""

import logging
from collections.abc import Callable

from pcbasm.gcode import GCode
from pcbasm.geometry import Point2d, Transform
from pcbasm.hal import Klipper, Speed, XYZStage
from pcbasm.utils import get_class_module_path


class XYPositionAdjustor:
    """XY 位置を反復的に補正するクラス.

    大まかに位置合わせした状態から、観測した想定→観測の Transform
    （カメラ mm 空間、原点=画像中心）をもとに、許容誤差内に収束するまで
    位置を微調整する。

    Example:
        adjustor = XYPositionAdjustor(
            observe=observer.observe,
            klipper=klipper,
            stage=stage,
            offset_transform=offset_transform,
            tolerance=0.01,
            settle_sec=machine.settle.move_sec,
        )
        final_pos = adjustor.adjust()
    """

    def __init__(
        self,
        observe: Callable[[], Transform],
        klipper: Klipper,
        stage: XYZStage,
        offset_transform: Transform,
        tolerance: float = 0.1,
        max_iterations: int = 10,
        move_velocity_ratio: float = 0.5,
        *,
        settle_sec: float,
    ) -> None:
        """XYPositionAdjustor を初期化する.

        Args:
            observe: 想定→観測の Transform（カメラ mm 空間）を返す関数
            klipper: Klipper クライアント
            stage: XYZ ステージ
            offset_transform: 観測オフセット系から機械座標系への変換
            tolerance: 許容誤差 (mm)
            max_iterations: 最大反復回数
            move_velocity_ratio: 最大速度に対する移動速度の割合 (0.0-1.0)
            settle_sec: 移動後の静定待ち [sec]（``machine.settle.move_sec``）
        """
        self._observe = observe
        self._klipper = klipper
        self._stage = stage
        self._offset_transform = offset_transform
        self._tolerance = tolerance
        self._max_iterations = max_iterations
        self._move_velocity_ratio = move_velocity_ratio
        self._settle_sec = settle_sec

        self._logger = logging.getLogger(get_class_module_path(self.__class__))

    def adjust(self) -> Point2d:
        """位置を反復的に補正する.

        毎回「現在位置 − 機械座標へ変換したオフセット」へ移動し、オフセットが
        ``tolerance`` 未満になったら終える。

        収束した回は移動しない。

        Returns:
            補正後の最終 XY 位置（機械座標、mm）。ステージの現在位置との差は tolerance 未満

        Raises:
            RuntimeError: 最大反復回数内に収束しなかった場合
            CircleDetectionError: ``observe`` が検出に失敗した場合（RuntimeError の派生）
        """
        self._logger.info("位置補正を開始")

        move_velocity = self._stage.max_velocity * self._move_velocity_ratio
        offset = Point2d(x=0.0, y=0.0)
        for iteration in range(self._max_iterations):
            offset = self._offset_transform.apply(
                self._observe().apply(Point2d(0.0, 0.0))
            )
            self._logger.info(
                f"試行 {iteration + 1}/{self._max_iterations}: "
                f"オフセット ({offset.x:.4f}, {offset.y:.4f}) mm, "
                f"距離 {offset.norm:.4f} mm"
            )

            pos = self._stage.get_position()
            target = pos.to2d() - offset

            if offset.norm < self._tolerance:
                self._logger.info(
                    f"許容誤差 {self._tolerance} mm 以内に収束: "
                    f"最終位置 ({target.x:.4f}, {target.y:.4f})"
                )
                return target

            # オフセット分だけ移動
            self._logger.debug(
                f"移動: ({pos.x:.4f}, {pos.y:.4f}) -> "
                f"({target.x:.4f}, {target.y:.4f})"
            )

            self._move_to(
                self._stage.move(
                    x=target.x, y=target.y, speed=Speed.absolute(move_velocity)
                ),
            )

        raise RuntimeError(
            f"{self._max_iterations}回の試行で収束しませんでした "
            f"(最終オフセット: {offset.norm:.4f} mm)"
        )

    def _move_to(self, move_gcode: GCode) -> None:
        """指定座標に移動し、安定を待つ."""
        self._klipper.send_gcode(
            move_gcode + GCode.wait(self._settle_sec) + GCode.wait_for_done()
        )
