import attrs

from pcb_assembly.geometry import Position, Waypoint

from .klipper import ReadonlyKlipper


@attrs.frozen
class AxisLimits:
    """軸の可動域を保持するクラス."""

    min: float
    max: float

    def __contains__(self, value: float) -> bool:
        """値が可動域内にあるか判定する.

        Args:
            value: 判定する値

        Returns:
            可動域内であればTrue
        """
        return self.min <= value <= self.max


@attrs.frozen
class Limits:
    """各軸の可動域と速度制限を保持するクラス."""

    x: AxisLimits
    y: AxisLimits
    z: AxisLimits
    v: AxisLimits

    def __contains__(self, waypoint: Waypoint) -> bool:
        """経由点が全軸の可動域・速度制限内にあるか判定する.

        Args:
            waypoint: 判定する経由点

        Returns:
            全制限内であればTrue
        """
        return (
            waypoint.x in self.x
            and waypoint.y in self.y
            and waypoint.z in self.z
            and waypoint.v in self.v
        )


class XYZStage:
    """XYZステージの状態を取得するクラス.

    Example:
        klipper = Klipper()
        stage = XYZStage(klipper.readonly)
        position = stage.get_position()
        limits = stage.get_limits()
    """

    def __init__(self, klipper: ReadonlyKlipper) -> None:
        """XYZStageを初期化する.

        Args:
            klipper: Klipperクライアント
        """
        self._klipper = klipper

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

        printer_key = "printer"
        if printer_key not in config:
            raise KeyError("printer.cfgに[printer]セクションを追加してください")
        printer = config[printer_key]
        if "max_velocity" not in printer:
            raise KeyError("printer.cfgの[printer]にmax_velocityを追加してください")

        return Limits(
            x=get_axis_limits("x"),
            y=get_axis_limits("y"),
            z=get_axis_limits("z"),
            v=AxisLimits(min=0.0, max=float(printer["max_velocity"])),
        )
