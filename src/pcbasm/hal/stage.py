from __future__ import annotations

from functools import cached_property
from typing import Self

import attrs

from pcbasm import gcode
from pcbasm.config import Machine
from pcbasm.geometry import Path, Point3d
from pcbasm.xy_calibration import XYCalibrationResult, XYCalibrationTransform

from .klipper import ReadonlyKlipper


@attrs.frozen
class Speed:
    """送り速度を表すイミュータブルなクラス.

    絶対値[mm/s]またはmax_velocityに対する割合[0,1]で表現する。
    インスタンスは ``absolute`` / ``rate`` クラスメソッド経由で生成する。

    Attributes:
        _value: 速度の値（絶対値[mm/s]または割合[0,1]）
        _is_fraction: 値が割合表現かどうか
    """

    _value: float = attrs.field(alias="value")
    _is_fraction: bool = attrs.field(alias="is_fraction")

    @classmethod
    def absolute(cls, mm_per_s: float) -> Self:
        """絶対速度[mm/s]からSpeedを生成する.

        Args:
            mm_per_s: 送り速度[mm/s]

        Returns:
            絶対速度を表すSpeedインスタンス
        """
        return cls(value=mm_per_s, is_fraction=False)

    @classmethod
    def rate(cls, fraction: float) -> Self:
        """max_velocityに対する割合[0,1]からSpeedを生成する.

        Args:
            fraction: max_velocityに対する割合（0以上1以下）

        Returns:
            割合表現のSpeedインスタンス

        Raises:
            ValueError: fractionが[0, 1]の範囲外の場合
        """
        if not 0.0 <= fraction <= 1.0:
            msg = f"rateは[0, 1]の範囲内である必要があります。与えられた値: {fraction}"
            raise ValueError(msg)
        return cls(value=fraction, is_fraction=True)

    def resolve(self, max_velocity: float) -> float:
        """最大速度を用いて速度を絶対値[mm/s]に解決する.

        Args:
            max_velocity: ステージの最大速度[mm/s]

        Returns:
            解決された絶対速度[mm/s]
        """
        if self._is_fraction:
            return self._value * max_velocity
        return self._value


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

    def __init__(
        self,
        klipper: ReadonlyKlipper,
        xy_transform: XYCalibrationTransform | None = None,
    ) -> None:
        """XYZStageを初期化する.

        Args:
            klipper: Klipperクライアント
        """
        self._klipper = klipper
        self._xy_transform = xy_transform or XYCalibrationTransform.identity()

    def get_position(self) -> Point3d:
        """現在位置を取得する.

        Returns:
            現在の座標
        """
        pos = self._klipper.get_status("gcode_move", "gcode_position")
        logical = self._xy_transform.inverse(Point3d(pos[0], pos[1], 0.0).to2d())
        return Point3d(x=logical.x, y=logical.y, z=pos[2])

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

        指定した軸のみをG1に含め、None軸は出力しない（未指定軸は機械側が
        現在位置に保持する）。relative=Trueは現在位置に対する相対移動として
        絶対座標に解決する。speed=Noneのときmax_velocityで解決する。
        指定軸とfeedをlimitsで検証し、範囲外の場合はValueErrorを送出する。

        Args:
            x: X座標（Noneは移動しない）
            y: Y座標（Noneは移動しない）
            z: Z座標（Noneは移動しない）
            speed: 送り速度（Noneのときmax_velocity）
            relative: 相対移動フラグ

        Returns:
            移動のGCode（指定軸のみ）

        Raises:
            ValueError: 全軸が未指定の場合、移動先またはfeedが制限外の場合
        """
        if x is None and y is None and z is None:
            raise ValueError("移動する軸が指定されていません")

        feed = float(
            (speed if speed is not None else Speed.rate(1.0)).resolve(self.max_velocity)
        )

        nx, ny, nz = x, y, z
        if relative:
            current = self.get_position()
            nx = current.x + x if x is not None else None
            ny = current.y + y if y is not None else None
            nz = current.z + z if z is not None else None

        raw_x: float | None = float(nx) if nx is not None else None
        raw_y: float | None = float(ny) if ny is not None else None
        if not self._xy_transform.is_identity and (nx is not None or ny is not None):
            if nx is None or ny is None:
                current = self.get_position()
                nx = current.x if nx is None else nx
                ny = current.y if ny is None else ny
            transformed = self._xy_transform.apply(
                Point3d(float(nx), float(ny), 0.0).to2d()
            )
            raw_x, raw_y = transformed.x, transformed.y

        targets: dict[str, float | None] = {
            "x": raw_x,
            "y": raw_y,
            "z": float(nz) if nz is not None else None,
        }
        axis_limits = {"x": self.limits.x, "y": self.limits.y, "z": self.limits.z}
        violations = [
            f"{axis}={value}"
            for axis, value in targets.items()
            if value is not None and value not in axis_limits[axis]
        ]
        if feed not in self.limits.v:
            violations.append(f"feed={feed}")
        if violations:
            raise ValueError(f"制限外の移動先です: {', '.join(violations)}")

        return gcode.move(x=targets["x"], y=targets["y"], z=targets["z"], velocity=feed)

    def to_gcode(self, path: Path, *, speed: Speed) -> gcode.GCode:
        """Path の各点を G1 移動に変換する。各点と feed を limits 検証する.

        Raises:
            ValueError: 制限外の点がある場合
        """
        feed = float(speed.resolve(self.max_velocity))
        raw_path = self._xy_transform.transform_path(path)
        invalid = [p for p in raw_path if not self.limits.contains(p, feed)]
        if invalid:
            raise ValueError(f"制限外の経由点があります: {invalid}")
        commands = gcode.GCode()
        for point in raw_path:
            commands.append(
                gcode.move(
                    x=float(point.x), y=float(point.y), z=float(point.z), velocity=feed
                )
            )
        return commands


def create_xyz_stage(
    machine: Machine,
    klipper: ReadonlyKlipper,
    *,
    calibrated: bool = True,
) -> XYZStage:
    """Machine設定に応じたXY補正付きStageを構築する.

    設定が無い場合は従来どおりidentityになる。設定があるのにファイルが 読めない場合は例外を伝播し、未補正のまま装置を動かさない。
    """
    transform = XYCalibrationTransform.identity()
    if calibrated and machine.xy_calibration is not None:
        transform = XYCalibrationResult.load(
            machine.xy_calibration.calibration_file
        ).transform
    return XYZStage(klipper, transform)
