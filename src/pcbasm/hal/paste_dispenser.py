from .air_pump import AirPump
from .klipper import GCode, ReadonlyKlipper
from .manual_stepper import ManualStepper


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
            RuntimeError: printer.cfgにmanual_stepperセクションまたは
                air_pumpセクションがない場合
        """
        self._stepper = ManualStepper(klipper, stepper_name)
        self._air_pump = AirPump(klipper)
        self._rotations_per_ul = rotations_per_ul

    @property
    def rotations_per_ul(self) -> float:
        """体積指令を回転数へ変換する係数 [rev/μL]."""
        return self._rotations_per_ul

    def set_rotations_per_ul(self, rotations_per_ul: float) -> None:
        """換算係数を差し替える（運転中の再キャリブレーション用）.

        AirPump / Stepper を開いたまま係数だけを変える。

        作り直すと AirPump の OFF→ON が挟まり、補正したい吐出そのものを乱す。
        """
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

        先に位置を 0 へリセットしてから動かすので、``amount`` は今の位置からの相対量として働く。

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

    def continue_pushpull(
        self,
        current_amount: float,
        amount: float,
        rate: float,
        accel: float,
        *,
        sync: bool = True,
    ) -> GCode:
        """直前の非同期pushpullと同じ座標系で吐出・リトラクションを続ける.

        ``SET_POSITION`` を挟まず、直前の目標 ``current_amount`` から
        ``amount`` だけ移動した絶対位置を次の目標にする。非同期吐出の完了前に
        リトラクションをqueueするとき、実行中の座標系を壊さず連続動作にできる。

        Args:
            current_amount: 直前のpushpullが指令した目標量 [μL]
            amount: 追加移動量 [μL]（正: 吐出、負: リトラクション）
            rate: 速度 [μL/sec]
            accel: 加速度 [μL/sec²]
            sync: Trueの場合、動作完了まで待機する

        Returns:
            連続吐出・リトラクション用のGCode
        """
        return self._stepper.rotate(
            self._ul_to_deg(current_amount + amount),
            self._ul_to_deg(rate),
            self._ul_to_deg(accel),
            sync=sync,
        )

    def sync(self) -> GCode:
        """先行するディスペンサー動作と後続G-codeの時刻を同期する."""
        return self._stepper.sync()

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
