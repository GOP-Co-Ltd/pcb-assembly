from __future__ import annotations

from functools import cached_property

import attrs

from pcbasm import gcode
from pcbasm.geometry import Path, Point3d

from .klipper import ReadonlyKlipper
from .speed import Speed


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

    def contains(self, point: Point3d, feed: float) -> bool:
        """点とfeedが全軸の可動域・速度制限内か判定する.

        Args:
            point: 判定する座標
            feed: 判定する送り速度[mm/s]

        Returns:
            全制限内であればTrue
        """
        return (
            point.x in self.x
            and point.y in self.y
            and point.z in self.z
            and feed in self.v
        )


class XYZStage:
    """XYZステージの状態を取得するクラス.

    Example:
        klipper = Klipper()
        stage = XYZStage(klipper.readonly)
        position = stage.get_position()
        limits = stage.limits
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

    @cached_property
    def limits(self) -> Limits:
        """各軸の可動域.

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

    @property
    def max_velocity(self) -> float:
        """最大速度."""
        return self.limits.v.max

    def move(
        self,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        *,
        speed: Speed | None = None,
        relative: bool = False,
    ) -> gcode.GCode:
        """単点移動のG-codeを生成する.

        None座標は現在位置を維持し、relative=Trueは相対移動として扱う。
        speed=Noneのときmax_velocityで解決する。解決後の点とfeedをlimitsで
        検証し、範囲外の場合はValueErrorを送出する。

        Args:
            x: X座標（Noneは現在位置を維持、relative時は0.0）
            y: Y座標（Noneは現在位置を維持、relative時は0.0）
            z: Z座標（Noneは現在位置を維持、relative時は0.0）
            speed: 送り速度（Noneのときmax_velocity）
            relative: 相対移動フラグ

        Returns:
            移動のGCode

        Raises:
            ValueError: 移動先またはfeedが制限外の場合
        """
        feed = float(
            (speed if speed is not None else Speed.rate(1.0)).resolve(self.max_velocity)
        )

        if relative or x is None or y is None or z is None:
            current = self.get_position()
            if relative:
                nx = current.x + (x if x is not None else 0.0)
                ny = current.y + (y if y is not None else 0.0)
                nz = current.z + (z if z is not None else 0.0)
            else:
                nx = x if x is not None else current.x
                ny = y if y is not None else current.y
                nz = z if z is not None else current.z
        else:
            nx, ny, nz = x, y, z

        point = Point3d(float(nx), float(ny), float(nz))
        if not self.limits.contains(point, feed):
            raise ValueError(f"制限外の移動先です: {point}, feed={feed}")

        return gcode.move(x=point.x, y=point.y, z=point.z, velocity=feed)

    def to_gcode(self, path: Path, *, speed: Speed) -> gcode.GCode:
        """Path の各点を G1 移動に変換する。各点と feed を limits 検証する.

        Raises:
            ValueError: 制限外の点がある場合
        """
        feed = float(speed.resolve(self.max_velocity))
        invalid = [p for p in path if not self.limits.contains(p, feed)]
        if invalid:
            raise ValueError(f"制限外の経由点があります: {invalid}")
        commands = gcode.GCode()
        for point in path:
            commands.append(
                gcode.move(
                    x=float(point.x), y=float(point.y), z=float(point.z), velocity=feed
                )
            )
        return commands
