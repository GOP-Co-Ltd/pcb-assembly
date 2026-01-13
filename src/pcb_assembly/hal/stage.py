import attrs

from pcb_assembly.hal.klipper import ReadonlyKlipper


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

    def __init__(self, klipper: ReadonlyKlipper) -> None:
        """XYZStageを初期化する.

        Args:
            klipper: Klipperクライアント
        """
        self._klipper = klipper

    def _check_macro_exists(self) -> None:
        """必要なマクロが定義されているか確認する.

        Raises:
            RuntimeError: 必要なマクロが定義されていない場合
        """
        for name in ["PRESENT"]:
            if not self._klipper.has_macro(name):
                raise RuntimeError(
                    f"printer.cfgに[gcode_macro {name}]を追加してください"
                )

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
