"""ペースト塗布の制御."""

import logging
import math
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
    generate_fill_path,
)
from pcb_assembly.hal import Klipper, PasteDispenser, XYZStage
from pcb_assembly.hal.paste_dispenser import NOZZLE_SPECS
from pcb_assembly.utils import get_class_module_path


def _trapezoidal_time(distance: float, rate: float, accel: float) -> float:
    """台形速度プロファイルで距離を走行する時間を計算する.

    速度0から加速し、rateに達したら定速走行する。

    Args:
        distance: 走行距離
        rate: 最大速度
        accel: 加速度

    Returns:
        走行時間 [sec]
    """
    d_accel = rate**2 / (2 * accel)
    if distance <= d_accel:
        return math.sqrt(2 * distance / accel)
    return rate / accel + (distance - d_accel) / rate


class PasteApplicator:
    """ポリゴンへのジグザグフィル塗布によるペースト塗布を制御するクラス.

    ペーストのローディング・リトラクション・塗布を一貫して提供する。
    各ポリゴンに対してジグザグ経路を生成し、ステージ移動と同期して連続吐出を行う。
    プライムと吐出は1つの連続ステッパー動作として実行し、G4でプライム時間分
    待機した後にステージ移動を開始する。

    Example:
        with PasteApplicator(
            klipper=klipper,
            paste_dispenser=dispenser,
            stage=stage,
            nozzle_size="23G",
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
        nozzle_size: str,
        dispense_rate: float,
        dispense_accel: float,
        ul_per_mm2: float,
        retraction: float,
        retraction_rate: float,
        retraction_accel_factor: float,
        transform: Transform = Identity(),
        paste_height: float = 0.1,
        lift_height: float = 5.0,
        perimeters: int = 1,
        prime_extra_delay: float = 0.0,
    ) -> None:
        """PasteApplicatorを初期化する.

        Args:
            klipper: Klipperクライアント
            paste_dispenser: ペーストディスペンサーHAL
            stage: XYZステージ
            nozzle_size: ノズルサイズ（例: "23G"）
            dispense_rate: 吐出レート [μL/sec]
            dispense_accel: 吐出加速度 [μL/sec²]
            ul_per_mm2: 1mm²あたりの塗布量 [μL/mm²]
            retraction: リトラクション量 [μL]
            retraction_rate: リトラクション速度 [μL/sec]
            retraction_accel_factor: リトラクション加速度係数（>1.0）
            transform: 座標変換
            paste_height: 塗布面のZ高さ [mm]
            lift_height: 塗布後の上昇高さ [mm]
            perimeters: 外周の周回数（デフォルト: 1）
            prime_extra_delay: プライム後の追加遅延 [sec]（デフォルト: 0.0）

        Raises:
            ValueError: retraction_accel_factorが1.0以下の場合
            ValueError: nozzle_sizeが無効な場合
        """
        if retraction_accel_factor <= 1.0:
            raise ValueError(
                f"retraction_accel_factorは1.0より大きい必要があります: "
                f"{retraction_accel_factor}"
            )
        if nozzle_size not in NOZZLE_SPECS:
            raise ValueError(
                f"無効なnozzle_sizeです: {nozzle_size} "
                f"(有効値: {', '.join(NOZZLE_SPECS)})"
            )

        nozzle_spec = NOZZLE_SPECS[nozzle_size]
        self._klipper = klipper
        self._paste_dispenser = paste_dispenser
        self._stage = stage
        self._nozzle_inner_diameter = nozzle_spec.inner_diameter
        self._dispense_rate = dispense_rate
        self._dispense_accel = dispense_accel
        self._ul_per_mm2 = ul_per_mm2
        self._transform = transform
        self._paste_height = paste_height
        self._retraction = retraction
        self._retraction_rate = retraction_rate
        self._retraction_accel_factor = retraction_accel_factor
        self._lift_height = lift_height
        self._perimeters = perimeters
        self._prime_extra_delay = prime_extra_delay
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

        各ポリゴンに対してジグザグフィル経路を生成し、
        ステージ移動と同期して連続吐出を行う（ブロッキング）。

        Args:
            polygons: 塗布対象のポリゴン群
        """
        for polygon in polygons:
            self._fill(polygon)

    def _fill(self, polygon: Polygon) -> None:
        """ポリゴンをジグザグフィル経路で塗布する."""
        fill_path = generate_fill_path(
            polygon,
            line_spacing=self._nozzle_inner_diameter,
            perimeters=self._perimeters,
            inset=self._nozzle_inner_diameter / 2,
        )

        if not fill_path:
            self._logger.warning("フィルパスが空です。スキップします。")
            return

        # 各点を変換して3D化
        transformed = [
            self._transform.apply(p.to3d(self._paste_height)) for p in fill_path
        ]

        # 吐出パラメータ算出
        total_amount = polygon.area * self._ul_per_mm2
        dispense_time = total_amount / self._dispense_rate

        # Trajectoryを構築し経路長を算出
        first = transformed[0]
        fill_trajectory = Trajectory(origin=first, initial_velocity=1.0)
        fill_trajectory.add(move=[Move.from_point(p, v=1.0) for p in transformed[1:]])
        path_length = fill_trajectory.distance()

        # プライム時間: 台形速度プロファイルで retraction 分を吐出する時間
        prime_time = _trapezoidal_time(
            self._retraction, self._dispense_rate, self._dispense_accel
        )

        max_v = self._stage.max_velocity
        lifted_z = first.z + self._lift_height
        last = transformed[-1]
        gc = gcode.GCode()

        # 1. 最初のポイント上空へ移動 → Z下降
        descent = Trajectory(
            origin=Point3d(first.x, first.y, lifted_z),
            initial_velocity=max_v,
        )
        descent.add(
            Move(x=first.x, y=first.y, z=lifted_z, v=max_v),
            Move(z=first.z, v=max_v),
        )
        gc.append(self._stage.to_gcode(descent))
        gc.append(gcode.wait_for_done())

        # 2. プライム+吐出を1つの連続動作として非同期開始
        # extra delay中もディスペンサーは動き続けるため、その分の吐出量を加算
        extra_amount = self._dispense_rate * self._prime_extra_delay
        gc.append(
            self._paste_dispenser.pushpull(
                self._retraction + extra_amount + total_amount,
                self._dispense_rate,
                self._dispense_accel,
                sync=False,
            )
        )

        # 3. プライム後にステージ移動（距離0の場合はその点で待機）
        if path_length > 0 and dispense_time > 0:
            fill_trajectory = fill_trajectory.with_velocity(path_length / dispense_time)
            gc.append(gcode.wait(prime_time + self._prime_extra_delay))
            gc.append(self._stage.to_gcode(fill_trajectory))
        else:
            gc.append(gcode.wait(prime_time + self._prime_extra_delay + dispense_time))
        gc.append(gcode.wait_for_done())

        # 4. リトラクション
        gc.append(
            self._paste_dispenser.pushpull(
                -self._retraction, self._retraction_rate, self._retraction_accel
            )
        )

        # 5. Z上昇
        ascent = Trajectory(
            Move(z=last.z + self._lift_height, v=max_v),
            origin=last,
            initial_velocity=max_v,
        )
        gc.append(self._stage.to_gcode(ascent))

        self._klipper.send_gcode(gc)
