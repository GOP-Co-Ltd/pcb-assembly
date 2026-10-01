from .air_pump import AirPump
from .klipper import GCode, ReadonlyKlipper
from .manual_stepper import ManualStepper


class PasteDispenser:
    """はんだペーストディスペンサーの HAL (オーガースクリュー方式).

    マイクロリットル [μL] 単位の API を提供します。内部では ManualStepper と AirPump
    を使い、rotations_per_ul で μL を回転数に変換します。
    """

    def __init__(
        self,
        klipper: ReadonlyKlipper,
        rotations_per_ul: float,
        stepper_name: str = "paste_dispenser",
    ) -> None:
        """PasteDispenser を初期化する.

        Args:
            klipper: Klipper クライアント
            rotations_per_ul: 1 μL あたりのステッパー回転数 [rev/μL]
            stepper_name: manual_stepper の名前

        Raises:
            RuntimeError: printer.cfg に manual_stepper セクションまたは
                air_pump セクションがない場合
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

        AirPump / Stepper を作り直さず、係数だけを変える。 作り直すと AirPump の OFF→ON
        が挟まり、補正対象の吐出そのものに影響する。
        """
        self._rotations_per_ul = rotations_per_ul

    def _ul_to_deg(self, microl: float) -> float:
        """マイクロリットル単位を角度に変換."""
        return microl * self._rotations_per_ul * 360

    def enable(self) -> GCode:
        """ディスペンサーを有効化する GCode を生成する（AirPump ON + Stepper Enable）."""
        return self._air_pump.on() + self._stepper.enable()

    def disable(self) -> GCode:
        """ディスペンサーを無効化する GCode を生成する（AirPump OFF + Stepper Disable）."""
        return self._air_pump.off() + self._stepper.disable()

    def pushpull(
        self,
        amount: float,
        rate: float,
        accel: float,
        *,
        sync: bool = True,
    ) -> GCode:
        """ペーストを吐出/リトラクションする GCode を生成する.

        先に位置を 0 へリセットしてから動かすので、``amount`` は今の位置からの
        相対量になる。

        Args:
            amount: 吐出量 [μL]（正: 吐出、負: リトラクション）
            rate: 速度 [μL/sec]
            accel: 加速度 [μL/sec²]
            sync: True の場合、動作完了まで待機する（デフォルト: True）

        Returns:
            吐出/リトラクション用の GCode
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
        """直前の非同期 pushpull と同じ座標系で吐出・リトラクションを続ける.

        ``SET_POSITION`` を挟まず、直前の目標 ``current_amount`` から
        ``amount`` だけ移動した絶対位置を次の目標にする。非同期吐出の完了前に
        リトラクションを queue するとき、実行中の座標系をリセットせずに連続動作させられる。

        Args:
            current_amount: 直前の pushpull が指令した目標量 [μL]
            amount: 追加移動量 [μL]（正: 吐出、負: リトラクション）
            rate: 速度 [μL/sec]
            accel: 加速度 [μL/sec²]
            sync: True の場合、動作完了まで待機する

        Returns:
            連続吐出・リトラクション用の GCode
        """
        return self._stepper.rotate(
            self._ul_to_deg(current_amount + amount),
            self._ul_to_deg(rate),
            self._ul_to_deg(accel),
            sync=sync,
        )

    def sync(self) -> GCode:
        """先行するディスペンサー動作と後続 G-code の時刻を同期する."""
        return self._stepper.sync()

    def rotate_revolutions(
        self,
        rotations: float,
        rate: float,
        accel: float,
        *,
        sync: bool = True,
    ) -> GCode:
        """流量キャリブレーション用に N 回転をオーガースクリューに実行させる GCode.

        rotations_per_ul が未知の場面（キャリブレーション）でも使えるよう、
        μL 単位を経由せず回転数で直接指定する。

        Args:
            rotations: 回転数 [rev]
            rate: 角速度 [rev/sec]
            accel: 角加速度 [rev/sec²]
            sync: True の場合、動作完了まで待機する（デフォルト: True）

        Returns:
            キャリブレーション用の GCode
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
