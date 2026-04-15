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
    generate_fill_path,
)
from pcb_assembly.hal import Klipper, PasteDispenser, XYZStage
from pcb_assembly.hal.paste_dispenser import NOZZLE_SPECS
from pcb_assembly.utils import get_class_module_path


class PasteApplicator:
    """ポリゴンへのジグザグフィル塗布によるペースト塗布を制御するクラス.

    ペーストのローディング・リトラクション・塗布を一貫して提供する。
    各ポリゴンに対してジグザグ経路を生成し、ステージ移動と同期して連続吐出を行う。

    Example:
        with PasteApplicator(
            klipper=klipper,
            paste_dispenser=dispenser,
            stage=stage,
            nozzle_size="23G",
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
        dispense_accel: float,
        ul_per_mm2: float,
        retraction: float,
        retraction_rate: float,
        retraction_accel_factor: float,
        transform: Transform = Identity(),
        paste_height: float = 0.1,
        lift_height: float = 5.0,
        fill_velocity: float | None = None,
        perimeters: int = 1,
    ) -> None:
        """PasteApplicatorを初期化する.

        Args:
            klipper: Klipperクライアント
            paste_dispenser: ペーストディスペンサーHAL
            stage: XYZステージ
            nozzle_size: ノズルサイズ（例: "23G"）
            dispense_accel: 吐出加速度 [μL/sec²]
            ul_per_mm2: 1mm²あたりの塗布量 [μL/mm²]
            retraction: リトラクション量 [μL]
            retraction_rate: リトラクション速度 [μL/sec]
            retraction_accel_factor: リトラクション加速度係数（>1.0）
            transform: 座標変換
            paste_height: 塗布面のZ高さ [mm]
            lift_height: 塗布後の上昇高さ [mm]
            fill_velocity: フィル時のXY速度 [mm/sec]（None時はstage.max_velocity）
            perimeters: 外周の周回数（デフォルト: 1）

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
        self._dispense_accel = dispense_accel
        self._ul_per_mm2 = ul_per_mm2
        self._transform = transform
        self._paste_height = paste_height
        self._retraction = retraction
        self._retraction_rate = retraction_rate
        self._retraction_accel_factor = retraction_accel_factor
        self._lift_height = lift_height
        self._fill_velocity = fill_velocity
        self._perimeters = perimeters
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

        max_v = self._stage.max_velocity
        fill_v = self._fill_velocity if self._fill_velocity is not None else max_v

        # フィル用Trajectoryを構築
        first = transformed[0]
        fill_trajectory = Trajectory(origin=first, initial_velocity=fill_v)
        fill_trajectory.add(
            move=[Move.from_point(p, v=fill_v) for p in transformed[1:]]
        )

        # 吐出パラメータ算出
        total_amount = polygon.area * self._ul_per_mm2
        traversal_time = fill_trajectory.time()
        if traversal_time <= 0:
            self._logger.warning("経路の所要時間が0です。スキップします。")
            return
        dispense_rate = total_amount / traversal_time

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

        # 2. プライム
        gc.append(
            self._paste_dispenser.pushpull(
                self._retraction, self._retraction_rate, self._retraction_accel
            )
        )

        # 3. 非同期吐出開始 + ステージ移動（並行実行）
        gc.append(
            self._paste_dispenser.pushpull(
                total_amount, dispense_rate, self._dispense_accel, sync=False
            )
        )
        gc.append(self._stage.to_gcode(fill_trajectory))
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
