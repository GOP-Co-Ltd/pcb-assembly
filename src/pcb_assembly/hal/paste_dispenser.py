import attrs

from .air_pump import AirPump
from .klipper import GCode, ReadonlyKlipper
from .manual_stepper import ManualStepper


@attrs.frozen
class NozzleSpec:
    """ディスペンサーノズルの仕様.

    Attributes:
        inner_diameter: 内径 [mm]
        outer_diameter: 外径 [mm]
    """

    inner_diameter: float
    outer_diameter: float


NOZZLE_SPECS: dict[str, NozzleSpec] = {
    "18G": NozzleSpec(inner_diameter=0.92, outer_diameter=1.28),
    "19G": NozzleSpec(inner_diameter=0.72, outer_diameter=1.08),
    "20G": NozzleSpec(inner_diameter=0.61, outer_diameter=0.91),
    "21G": NozzleSpec(inner_diameter=0.52, outer_diameter=0.82),
    "22G": NozzleSpec(inner_diameter=0.42, outer_diameter=0.72),
    "23G": NozzleSpec(inner_diameter=0.34, outer_diameter=0.64),
    "24G": NozzleSpec(inner_diameter=0.31, outer_diameter=0.57),
    "25G": NozzleSpec(inner_diameter=0.26, outer_diameter=0.52),
    "26G": NozzleSpec(inner_diameter=0.25, outer_diameter=0.46),
    "27G": NozzleSpec(inner_diameter=0.21, outer_diameter=0.42),
}


class PasteDispenser:
    """はんだペーストディスペンサーのHAL (オーガースクリュー方式).

    マイクロリットル [μL] 単位のAPIを提供します。 内部的に ManualStepper と AirPump
    を利用し、rotations_per_ul で μL → 回転数に変換します。
    """

    def __init__(
        self,
        klipper: ReadonlyKlipper,
        rotations_per_ul: float,
        stepper_name: str = "paste_dispenser",
    ) -> None:
        """PasteDispenserを初期化する.

        Args:
            klipper: Klipperクライアント
            rotations_per_ul: 1μLあたりのステッパー回転数 [rev/μL]
            stepper_name: manual_stepperの名前

        Raises:
            RuntimeError: printer.cfgにmanual_stepperセクションまたはair_pumpセクションがない場合
        """
        self._stepper = ManualStepper(klipper, stepper_name)
        self._air_pump = AirPump(klipper)
        self._rotations_per_ul = rotations_per_ul

    def _ul_to_deg(self, microl: float) -> float:
        """マイクロリットル単位を角度に変換."""
        return microl * self._rotations_per_ul * 360

    def enable(self) -> GCode:
        """ディスペンサーを有効化するGCodeを生成する（AirPump ON + Stepper Enable）."""
        return self._air_pump.on() + self._stepper.enable()

    def disable(self) -> GCode:
        """ディスペンサーを無効化するGCodeを生成する（AirPump OFF + Stepper Disable）."""
        return self._air_pump.off() + self._stepper.disable()

    def pushpull(
        self,
        amount: float,
        rate: float,
        accel: float,
        *,
        sync: bool = True,
    ) -> GCode:
        """ペーストを吐出/リトラクションするGCodeを生成.

        Args:
            amount: 吐出量 [μL]（正: 吐出、負: リトラクション）
            rate: 速度 [μL/sec]
            accel: 加速度 [μL/sec²]
            sync: Trueの場合、動作完了まで待機する（デフォルト: True）

        Returns:
            吐出/リトラクション用のGCode
        """
        gcode = self._stepper.reset_position()
        gcode.append(
            self._stepper.rotate(
                self._ul_to_deg(amount),
                self._ul_to_deg(rate),
                self._ul_to_deg(accel),
                sync=sync,
            )
        )
        return gcode

    def rotate_revolutions(
        self,
        rotations: float,
        rate: float,
        accel: float,
        *,
        sync: bool = True,
    ) -> GCode:
        """流量キャリブレーション用にN回転をオーガースクリューに実行させるGCode.

        rotations_per_ul が未知の場面（キャリブレーション）でも使えるよう、
        μL単位を経由せず回転数で直接指定する。

        Args:
            rotations: 回転数 [rev]
            rate: 角速度 [rev/sec]
            accel: 角加速度 [rev/sec²]
            sync: Trueの場合、動作完了まで待機する（デフォルト: True）

        Returns:
            キャリブレーション用のGCode
        """
        gcode = self._stepper.reset_position()
        gcode.append(
            self._stepper.rotate(
                rotations * 360,
                rate * 360,
                accel * 360,
                sync=sync,
            )
        )
        return gcode
