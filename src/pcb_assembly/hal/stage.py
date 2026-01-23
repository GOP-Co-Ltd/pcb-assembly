from __future__ import annotations

import attrs

from pcb_assembly import gcode
from pcb_assembly.geometry import Point3d, Trajectory, Waypoint

from .klipper import ReadonlyKlipper


@attrs.frozen
class ScalarLimits:
    """スカラー値の範囲を保持するクラス."""

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

    x: ScalarLimits
    y: ScalarLimits
    z: ScalarLimits
    v: ScalarLimits

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

    def get_position(self) -> Point3d:
        """現在位置を取得する.

        Returns:
            現在の座標
        """
        pos = self._klipper.get_status("gcode_move", "gcode_position")
        return Point3d(x=pos[0], y=pos[1], z=pos[2])

    def get_limits(self) -> Limits:
        """各軸の可動域を取得する.

        Returns:
            各軸の可動域

        Raises:
            KeyError: 設定ファイルに必要なキーが無い場合
        """
        config = self._klipper.get_config()

        def get_axis_limits(axis: str) -> ScalarLimits:
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
            return ScalarLimits(
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
            v=ScalarLimits(min=0.0, max=float(printer["max_velocity"])),
        )

    def validate(self, trajectory: Trajectory) -> ValidationResult:
        """Trajectoryの全経由点が制限内にあるか検証する.

        Args:
            trajectory: 検証するTrajectory

        Returns:
            検証結果
        """
        limits = self.get_limits()
        invalid = [wp for wp in trajectory.waypoints if wp not in limits]
        return ValidationResult(invalid)

    def move(self, trajectory: Trajectory) -> gcode.GCode:
        """TrajectoryをG-codeに変換する.

        Args:
            trajectory: 変換するTrajectory

        Returns:
            移動のGCode

        Raises:
            ValueError: 制限外の経由点がある場合
        """
        result = self.validate(trajectory)
        if not result.is_valid:
            raise ValueError(f"制限外の経由点があります: {result.invalid_points}")

        commands = gcode.GCode()
        for wp in trajectory.waypoints:
            commands.append(gcode.move(x=wp.x, y=wp.y, z=wp.z, velocity=wp.v))
        return commands


@attrs.frozen
class ValidationResult:
    """Trajectory検証結果を保持するクラス."""

    _invalid_points: list[Waypoint]

    @property
    def is_valid(self) -> bool:
        """全経由点が制限内にあるか."""
        return len(self._invalid_points) == 0

    @property
    def invalid_points(self) -> list[Waypoint]:
        """制限外の経由点のリスト."""
        return self._invalid_points.copy()
