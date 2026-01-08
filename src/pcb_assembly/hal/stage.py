import attrs

from pcb_assembly.hal.klipper import Klipper


@attrs.define(slots=True, frozen=True)
class Position:
    """位置情報を保持するクラス."""

    x: float
    y: float
    z: float


@attrs.define(slots=True, frozen=True)
class AxisLimits:
    """軸の可動域を保持するクラス."""

    min: float
    max: float


@attrs.define(slots=True, frozen=True)
class Limits:
    """各軸の可動域を保持するクラス."""

    x: AxisLimits
    y: AxisLimits
    z: AxisLimits


class XYZStage:
    """XYZステージを制御するクラス.

    Example:
        klipper = Klipper("192.168.1.100")
        stage = XYZStage(klipper, default_speed=50.0)
        stage.home()
        stage.move(x=10, y=20, z=5)
        position = stage.get_position()
    """

    def __init__(self, klipper: Klipper, default_speed: float) -> None:
        """XYZStageを初期化する.

        Args:
            klipper: Klipperクライアント
            default_speed: デフォルトの移動速度 (mm/s)
        """
        self._klipper = klipper
        self._default_speed = default_speed

    def home(self) -> None:
        """全軸の原点復帰を行う."""
        self._klipper.send_gcode("G28 X Y Z")
        self._klipper.wait_for_move()

    def move(
        self,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        speed: float | None = None,
        relative: bool = False,
    ) -> None:
        """移動する.

        Args:
            x: X座標（Noneの場合は移動しない）
            y: Y座標（Noneの場合は移動しない）
            z: Z座標（Noneの場合は移動しない）
            speed: 移動速度 mm/s（Noneの場合はデフォルト速度）
            relative: 相対モードで動かす
        """
        if x is None and y is None and z is None:
            return

        speed_mm_s = speed if speed is not None else self._default_speed
        feedrate = speed_mm_s * 60  # mm/s -> mm/min

        codes = []
        if relative:
            codes.append("G91")
        else:
            codes.append("G90")
        parts = []
        parts.append("G1")
        if x is not None:
            parts.append(f"X{x}")
        if y is not None:
            parts.append(f"Y{y}")
        if z is not None:
            parts.append(f"Z{z}")
        parts.append(f"F{feedrate}")
        codes.append(" ".join(parts))
        codes.append("G90")  # 絶対座標に戻す

        self._klipper.send_gcode("\n".join(codes))
        self._klipper.wait_for_move()

    def get_position(self) -> Position:
        """現在位置を取得する.

        Returns:
            現在の座標
        """
        pos = self._klipper.get_status("gcode_move", "gcode_position")
        return Position(x=pos[0], y=pos[1], z=pos[2])

    def get_limits(self) -> Limits:
        """各軸の可動域を取得する.

        Returns:
            各軸の可動域

        Raises:
            KeyError: 設定ファイルに必要なキーが無い場合
        """
        config = self._klipper.get_config()

        def get_axis_limits(axis: str) -> AxisLimits:
            stepper_key = f"stepper_{axis}"
            if stepper_key not in config:
                raise KeyError(
                    f"printer.cfgに[{stepper_key}]セクションを追加してください"
                )
            stepper = config[stepper_key]
            if "position_min" not in stepper or "position_max" not in stepper:
                raise KeyError(
                    f"printer.cfgの[{stepper_key}]にposition_minとposition_maxを追加してください"
                )
            return AxisLimits(
                min=float(stepper["position_min"]),
                max=float(stepper["position_max"]),
            )

        return Limits(
            x=get_axis_limits("x"),
            y=get_axis_limits("y"),
            z=get_axis_limits("z"),
        )
