"""ペースト塗布の制御."""

import logging
from collections.abc import Iterable
from typing import Self

from shapely import Polygon

from pcb_assembly import gcode
from pcb_assembly.geometry import (
    Identity,
    Move,
    Point2d,
    Point3d,
    Trajectory,
    Transform,
)
from pcb_assembly.hal import Klipper, PasteDispenser, XYZStage
from pcb_assembly.utils import get_class_module_path


class PasteApplicator:
    """ポリゴンへのポイント吐出によるペースト塗布を制御するクラス.

    ペーストのローディング・リトラクション・塗布を一貫して提供する。
    各ポリゴンの重心にポイント吐出を行い、面積に応じた量を塗布する。

    Example:
        with PasteApplicator(
            klipper=klipper,
            paste_dispenser=dispenser,
            stage=stage,
            dispense_rate=5.0,
            dispense_accel=1.0,
            ul_per_mm2=0.05,
            retraction=10.0,
            retraction_rate=10.0,
            retraction_accel_factor=2.0,
            paste_height=0.5,
        ) as applicator:
            applicator.load(2.0)
            applicator.retract()
            applicator.apply([polygon])
    """

    def __init__(
        self,
        klipper: Klipper,
        paste_dispenser: PasteDispenser,
        stage: XYZStage,
        dispense_rate: float,
        dispense_accel: float,
        ul_per_mm2: float,
        retraction: float,
        retraction_rate: float,
        retraction_accel_factor: float,
        transform: Transform = Identity(),
        paste_height: float = 0.1,
        lift_height: float = 5.0,
    ) -> None:
        """PasteApplicatorを初期化する.

        Args:
            klipper: Klipperクライアント
            paste_dispenser: ペーストディスペンサーHAL
            stage: XYZステージ
            dispense_rate: ポイント吐出レート [μL/sec]
            dispense_accel: ポイント吐出加速度 [μL/sec²]
            ul_per_mm2: 1mm²あたりの塗布量 [μL/mm²]
            retraction: リトラクション量 [μL]
            retraction_rate: リトラクション速度 [μL/sec]
            retraction_accel_factor: リトラクション加速度係数（>1.0）
            transform: 座標変換
            paste_height: 塗布面のZ高さ [mm]
            lift_height: 塗布後の上昇高さ [mm]

        Raises:
            ValueError: retraction_accel_factorが1.0以下の場合
        """
        if retraction_accel_factor <= 1.0:
            raise ValueError(
                f"retraction_accel_factorは1.0より大きい必要があります: "
                f"{retraction_accel_factor}"
            )

        self._klipper = klipper
        self._paste_dispenser = paste_dispenser
        self._stage = stage
        self._dispense_rate = dispense_rate
        self._dispense_accel = dispense_accel
        self._ul_per_mm2 = ul_per_mm2
        self._transform = transform
        self._paste_height = paste_height
        self._retraction = retraction
        self._retraction_rate = retraction_rate
        self._retraction_accel_factor = retraction_accel_factor
        self._lift_height = lift_height
        self._logger = logging.getLogger(get_class_module_path(self.__class__))

    def __enter__(self) -> Self:
        """ディスペンサーを有効化する（AirPump ON + Stepper Enable）."""
        self._klipper.send_gcode(self._paste_dispenser.enable())
        return self

    def __exit__(self, *args: object) -> None:
        """ディスペンサーを無効化する（AirPump OFF + Stepper Disable）."""
        self._klipper.send_gcode(self._paste_dispenser.disable())

    @property
    def _retraction_accel(self) -> float:
        """リトラクション加速度 a_R [μL/sec²]."""
        return (
            self._retraction_accel_factor * self._retraction_rate**2 / self._retraction
        )

    def load(self, amount: float) -> None:
        """指定量のペーストを押し出す.

        GCodeを生成・送信し、動作完了まで待機（ブロッキング）する。

        Args:
            amount: 押し出し量 [μL]（正: 吐出、負: リトラクション）
        """
        self._logger.info(f"ペーストローディング: {amount} μL")
        push_gcode = self._paste_dispenser.pushpull(
            amount, self._retraction_rate, self._retraction_accel
        )
        self._klipper.send_gcode(push_gcode + gcode.wait_for_done())
        self._logger.info("ローディング完了")

    def retract(self) -> None:
        """リトラクションを実行する.

        retraction量分だけペーストを引き戻す（ブロッキング）。
        """
        self.load(-self._retraction)

    def apply(self, polygons: Iterable[Polygon]) -> None:
        """複数ポリゴンへペースト塗布を実行する.

        各ポリゴンの重心にポイント吐出を行う（ブロッキング）。

        Args:
            polygons: 塗布対象のポリゴン群
        """
        for polygon in polygons:
            centroid = polygon.centroid
            point = Point2d(centroid.x, centroid.y)
            amount = polygon.area * self._ul_per_mm2
            self._put(point, amount)

    def _put(self, point: Point2d, amount: float) -> None:
        """指定座標にポイント吐出を行う."""
        target = self._transform.apply(point.to3d(self._paste_height))
        lifted_z = target.z + self._lift_height
        max_v = self._stage.max_velocity

        gc = gcode.GCode()

        # 1. 目的地上空へ移動 → Z下降
        descent = Trajectory(
            origin=Point3d(target.x, target.y, lifted_z),
            initial_velocity=max_v,
        )
        descent.add(
            Move(x=target.x, y=target.y, z=lifted_z, v=max_v),
            Move(z=target.z, v=max_v),
        )
        gc.append(self._stage.to_gcode(descent))
        gc.append(gcode.wait_for_done())

        # 2. プライム → 吐出 → リトラクション
        gc.append(
            self._paste_dispenser.pushpull(
                self._retraction, self._retraction_rate, self._retraction_accel
            )
        )
        gc.append(
            self._paste_dispenser.pushpull(
                amount, self._dispense_rate, self._dispense_accel
            )
        )
        gc.append(
            self._paste_dispenser.pushpull(
                -self._retraction, self._retraction_rate, self._retraction_accel
            )
        )

        # 3. Z上昇
        ascent = Trajectory(
            Move(z=lifted_z, v=max_v),
            origin=Point3d(target.x, target.y, target.z),
            initial_velocity=max_v,
        )
        gc.append(self._stage.to_gcode(ascent))

        self._klipper.send_gcode(gc)
