from .air_pump import AirPump
from .klipper import GCode, ReadonlyKlipper
from .manual_stepper import ManualStepper


class PasteDispenser:
    """はんだペーストディスペンサーのHAL (オーガースクリュー方式).

    マイクロリットル [μL] 単位のAPIを提供します。 内部的に ManualStepper と AirPump
    を利用し、rotations_per_ul で μL → 回転数に変換します。

    ``air_pump_enabled=False`` のときは AirPump をインスタンス化せず、enable() /
    disable() の GCode から air_pump 制御を除外します。
    """

    def __init__(
        self,
        klipper: ReadonlyKlipper,
        rotations_per_ul: float,
        stepper_name: str = "paste_dispenser",
        air_pump_enabled: bool = True,
    ) -> None:
        """PasteDispenserを初期化する.

        Args:
            klipper: Klipperクライアント
            rotations_per_ul: 1μLあたりのステッパー回転数 [rev/μL]
            stepper_name: manual_stepperの名前
            air_pump_enabled: エアポンプの有効/無効。Falseの場合 AirPump を生成せず、
                air_pumpセクションの存在チェックも行わない

        Raises:
            RuntimeError: printer.cfgにmanual_stepperセクションがない場合。
                air_pump_enabled=True かつ air_pumpセクションがない場合も同様。
                air_pump_enabled=False のときは air_pumpセクションが無くても許容する。
        """
        self._stepper = ManualStepper(klipper, stepper_name)
        self._air_pump: AirPump | None = AirPump(klipper) if air_pump_enabled else None
        self._rotations_per_ul = rotations_per_ul

    def _ul_to_deg(self, microl: float) -> float:
        """マイクロリットル単位を角度に変換."""
        return microl * self._rotations_per_ul * 360

    def enable(self) -> GCode:
        """ディスペンサーを有効化するGCodeを生成する（AirPump ON + Stepper Enable）.

        air_pumpが無効の場合は Stepper Enable のみを返す。
        """
        stepper_gcode = self._stepper.enable()
        if self._air_pump is None:
            return stepper_gcode
        return self._air_pump.on() + stepper_gcode

    def disable(self) -> GCode:
        """ディスペンサーを無効化するGCodeを生成する（AirPump OFF + Stepper Disable）.

        air_pumpが無効の場合は Stepper Disable のみを返す。
        """
        stepper_gcode = self._stepper.disable()
        if self._air_pump is None:
            return stepper_gcode
        return self._air_pump.off() + stepper_gcode

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
