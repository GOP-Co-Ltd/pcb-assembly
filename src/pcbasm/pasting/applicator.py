"""ペースト塗布の制御."""

import logging
import math
from collections.abc import Iterable
from typing import Self

from shapely import Polygon

from pcbasm import gcode
from pcbasm.geometry import (
    Identity,
    Path,
    Transform,
)
from pcbasm.hal import Klipper, PasteDispenser, Speed, XYZStage
from pcbasm.pasting.fill_path import build_paste_fill_path
from pcbasm.pasting.fill_sequence import FillSequence
from pcbasm.utils import get_class_module_path


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
    """ポリゴンへの螺旋フィル塗布によるペースト塗布を制御するクラス.

    ペーストのローディング・リトラクション・塗布を一貫して提供する。
    各ポリゴンに対して螺旋経路を生成し、ステージ移動と同期して連続吐出を行う。
    プライムと吐出は1つの連続ステッパー動作として実行し、G4でプライム時間分
    待機した後にステージ移動を開始する。

    Example:
        with PasteApplicator(
            klipper=klipper,
            paste_dispenser=dispenser,
            stage=stage,
            nozzle_diameter=0.34,
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
        nozzle_diameter: float,
        dispense_rate: float,
        dispense_accel: float,
        ul_per_mm2: float,
        retraction: float,
        retraction_rate: float,
        retraction_accel_factor: float,
        transform: Transform = Identity(),
        paste_height: float = 0.1,
        lift_height: float = 5.0,
        prime_extra_delay: float = 0.0,
    ) -> None:
        """PasteApplicatorを初期化する.

        Args:
            klipper: Klipperクライアント
            paste_dispenser: ペーストディスペンサーHAL
            stage: XYZステージ
            nozzle_diameter: ノズル内径 [mm]（例: 0.34）
            dispense_rate: 吐出レート [μL/sec]
            dispense_accel: 吐出加速度 [μL/sec²]
            ul_per_mm2: 1mm²あたりの塗布量 [μL/mm²]
            retraction: リトラクション量 [μL]
            retraction_rate: リトラクション速度 [μL/sec]
            retraction_accel_factor: リトラクション加速度係数（>1.0）
            transform: 座標変換
            paste_height: 塗布面のZ高さ [mm]
            lift_height: 塗布後の上昇高さ [mm]
            prime_extra_delay: プライム後の追加遅延 [sec]（デフォルト: 0.0）

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
        self._nozzle_diameter = nozzle_diameter
        self._dispense_rate = dispense_rate
        self._dispense_accel = dispense_accel
        self._ul_per_mm2 = ul_per_mm2
        self._transform = transform
        self._paste_height = paste_height
        self._retraction = retraction
        self._retraction_rate = retraction_rate
        self._retraction_accel_factor = retraction_accel_factor
        self._lift_height = lift_height
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

    def calibrate(self, rotations: float, rate: float, accel: float) -> None:
        """流量キャリブレーション用にN回転を実行する（ブロッキング）.

        Args:
            rotations: 回転数 [rev]
            rate: 角速度 [rev/sec]
            accel: 角加速度 [rev/sec²]
        """
        self._logger.info(
            "キャリブレーション: %s回転 @ %s rev/s, accel=%s rev/s^2",
            rotations,
            rate,
            accel,
        )
        gc = self._paste_dispenser.rotate_revolutions(rotations, rate, accel)
        self._klipper.send_gcode(gc + gcode.wait_for_done())
        self._logger.info("キャリブレーション完了")

    def apply(self, polygons: Iterable[Polygon]) -> None:
        """複数ポリゴンへペースト塗布を実行する.

        各ポリゴンに対して螺旋フィル経路を生成し、
        ステージ移動と同期して連続吐出を行う（ブロッキング）。

        Args:
            polygons: 塗布対象のポリゴン群
        """
        for polygon in polygons:
            self._fill(polygon)

    def _fill(self, polygon: Polygon) -> None:
        """ポリゴンを螺旋フィル経路で塗布する."""
        raw = build_paste_fill_path(polygon, nozzle_diameter=self._nozzle_diameter)
        if not raw:
            self._logger.warning("フィルパスが空です。スキップします。")
            return

        path = Path(p.to3d(self._paste_height) for p in raw).transformed(
            self._transform
        )
        prime_time = _trapezoidal_time(
            self._retraction, self._dispense_rate, self._dispense_accel
        )
        sequence = FillSequence(
            path=path,
            total_amount=polygon.area * self._ul_per_mm2,
            retraction=self._retraction,
            extra_amount=self._dispense_rate * self._prime_extra_delay,
            dispense_rate=self._dispense_rate,
            dispense_accel=self._dispense_accel,
            retraction_rate=self._retraction_rate,
            retraction_accel=self._retraction_accel,
            prime_time=prime_time + self._prime_extra_delay,
            lift_height=self._lift_height,
            travel_speed=Speed.rate(1.0),
        )
        self._klipper.send_gcode(sequence.to_gcode(self._stage, self._paste_dispenser))
