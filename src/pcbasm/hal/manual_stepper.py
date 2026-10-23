from .klipper import GCode, ReadonlyKlipper


class ManualStepper:
    """Klipper manual_stepper の HAL.

    G-code を生成するだけで送信しない。
    Klipper の ``MANUAL_STEPPER MOVE=`` は絶対位置の指令なので、``move`` /
    ``rotate`` の位置引数はどちらも絶対位置である。
    相対量で動かすときは、先に ``reset_position()`` で現在位置を基準値に置く。
    """

    def __init__(self, klipper: ReadonlyKlipper, stepper_name: str) -> None:
        """ManualStepper を初期化する.

        Args:
            klipper: Klipper クライアント
            stepper_name: manual_stepper の名前

        Raises:
            RuntimeError: printer.cfg に manual_stepper セクションがない場合
        """
        self._klipper = klipper
        self._name = stepper_name
        self._check_klipper()

    @property
    def name(self) -> str:
        """ステッパー名を返す."""
        return self._name

    @property
    def _stepper_section(self) -> str:
        return f"manual_stepper {self._name}"

    @property
    def _cmd_prefix(self) -> str:
        return f"MANUAL_STEPPER STEPPER={self._name}"

    def _check_klipper(self) -> None:
        config = self._klipper.get_config()
        if self._stepper_section not in config:
            raise RuntimeError(
                f"printer.cfgに[{self._stepper_section}]を追加してください"
            )

    @property
    def rotation_distance(self) -> float:
        """rotation_distance [mm] を config から取得する."""
        config = self._klipper.get_config()
        return float(config[self._stepper_section]["rotation_distance"])

    def _deg_to_mm(self, deg: float) -> float:
        return deg / 360 * self.rotation_distance

    def rotate(
        self,
        angle: float,
        speed: float | None = None,
        accel: float | None = None,
        *,
        sync: bool = True,
    ) -> GCode:
        """回転の GCode を生成する.

        Args:
            angle: 目標角度 [deg]（``reset_position()`` を基準にした絶対値）
            speed: 角速度 [deg/s]（省略時は Klipper 設定値を使用）
            accel: 角加速度 [deg/s^2]（省略時は Klipper 設定値を使用）
            sync: True の場合、動作完了まで待機する（デフォルト: True）

        Returns:
            回転用の GCode
        """
        return self.move(
            self._deg_to_mm(angle),
            self._deg_to_mm(speed) if speed is not None else None,
            self._deg_to_mm(accel) if accel is not None else None,
            sync=sync,
        )

    def reset_position(self, position: float = 0.0) -> GCode:
        """位置をリセットする GCode を生成する.

        Args:
            position: リセット後の位置（デフォルト: 0.0）

        Returns:
            位置リセット用の GCode
        """
        return GCode(f"{self._cmd_prefix} SET_POSITION={position}")

    def move(
        self,
        distance: float,
        speed: float | None = None,
        accel: float | None = None,
        *,
        sync: bool = True,
    ) -> GCode:
        """移動の GCode を生成する.

        Args:
            distance: 目標位置 [mm]（``reset_position()`` を基準にした絶対値）
            speed: 速度 [mm/s]（省略時は Klipper 設定値を使用）
            accel: 加速度 [mm/s^2]（省略時は Klipper 設定値を使用）
            sync: True の場合、動作完了まで待機する（デフォルト: True）

        Returns:
            移動用の GCode
        """
        cmd = f"{self._cmd_prefix} MOVE={distance}"
        if speed is not None:
            cmd += f" SPEED={speed}"
        if accel is not None:
            cmd += f" ACCEL={accel}"
        if not sync:
            cmd += " SYNC=0"
        return GCode(cmd)

    def sync(self) -> GCode:
        """先行する manual stepper 動作と後続 G-code の時刻を同期する."""
        return GCode(f"{self._cmd_prefix} SYNC=1")

    def enable(self) -> GCode:
        """ステッパーを有効化する GCode を生成する.

        Returns:
            有効化用の GCode
        """
        return GCode(f"{self._cmd_prefix} ENABLE=1")

    def disable(self) -> GCode:
        """ステッパーを無効化する GCode を生成する.

        Returns:
            無効化用の GCode
        """
        return GCode(f"{self._cmd_prefix} ENABLE=0")
