"""Pnp-motion ファームで動かす XYZ ステージ（:class:`~pcbasm.hal.motion.MotionStage` の MCU
版）.

mm と mm/s をステップに直して :class:`~pcbasm.hal.mcu.firmware.PnpMotionFirmware` に送る。原点出しを
済ませた軸はホストが覚える（MCU は覚えない）。

決めたこと:

- **接続:** 作るときに開いて設定する（:func:`~pcbasm.hal.mcu.firmware.open_firmware`）。接続が切れたら、
  :meth:`McuMotionStage.restart` で開き直すまで、:meth:`~McuMotionStage.status` を含めて :class:`MotionError`
- **スレッド:** 操作は 1 つのロックで 1 つずつ実行する。:meth:`~McuMotionStage.emergency_stop` はロックを
  取らず（transport の送信のロックだけ）、原点出しや移動の待ちの最中でもすぐ送る。待っている操作は、
  MCU の shutdown を受けて :class:`MotionError` で終わる。:meth:`~McuMotionStage.status` はロックを取らず、
  最後に受けた報告を読むだけ。:meth:`~McuMotionStage.tare` もロックを取らない（MCU に何も送らない）
- **接触停止:** 原点出しの後、MCU は ``contact`` のまま残るが、止まっている（``IDLE``）として扱う。
  速度指令を受けたら ``velocity_stream_start`` で開始し直す
- **止まっているときの全軸 0:** 何も送らない（流し始めず、励磁も入れない）。脱力の後に 0 を送り続けても、
  脱力が取り消されない
- **座標指定の移動:** mm をステップに直して :meth:`PnpMotionFirmware.travel` に任せる（直線に動き、
  速さを省けばどの軸も ``max_velocity`` を超えない最も速い動き。1 ステップ以内の差は動かさない）
- **緊急停止が届かない間:** 次の間の :meth:`~McuMotionStage.emergency_stop` は :class:`MotionError` になる。
  どの間も MCU は流していないか、速度指令の期限（``stream_ttl``）で止まる
    - :meth:`~McuMotionStage.restart` の最中（閉じた接続を指している）と、restart に失敗した後
    - 接続が切れた後（:meth:`~McuMotionStage.restart` で開き直すまで）
    - :meth:`~McuMotionStage.close` の後（close が実行中の操作の終わりを待つ間は、まだ送れる）
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Collection, Iterator
from typing import override

from pcbasm.geometry import Point3d
from pcbasm.hal.mcu.errors import McuError, McuShutdown
from pcbasm.hal.mcu.firmware import (
    AXES,
    PnpMotionFirmware,
    ProcedureError,
    open_firmware,
)
from pcbasm.hal.mcu.homing import home_axis
from pcbasm.hal.mcu.settings import McuMotionSettings
from pcbasm.hal.motion import (
    Axis,
    MotionError,
    MotionStage,
    MotionState,
    MotionStatus,
    Velocity,
    homing_order,
)
from pcbasm.hal.stage import Limits, ScalarLimits


class McuMotionStage(MotionStage):
    """Pnp-motion ファームの MCU につないだ XYZ ステージ.

    作るときに MCU を開いて設定する（設定済みなら使い回す）。原点は「未」で始まる。

    Raises:
        MotionError: MCU を開けない、pnp-motion ファームではない、設定できない
    """

    def __init__(self, settings: McuMotionSettings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._homed: frozenset[Axis] = frozenset()
        self._failure: str | None = None
        self._limits = Limits(
            x=ScalarLimits(settings.x.position_min, settings.x.position_max),
            y=ScalarLimits(settings.y.position_min, settings.y.position_max),
            z=ScalarLimits(settings.z.position_min, settings.z.position_max),
            v=ScalarLimits(0.0, max(axis.max_velocity for axis in settings.axes)),
        )
        self._max_velocity = Velocity(
            settings.x.max_velocity, settings.y.max_velocity, settings.z.max_velocity
        )
        with self._translate():
            self._firmware = open_firmware(settings)

    @property
    @override
    def limits(self) -> Limits:
        return self._limits

    @property
    @override
    def max_velocity(self) -> Velocity:
        return self._max_velocity

    @override
    def status(self) -> MotionStatus:
        if self._failure is not None:
            raise MotionError(self._failure)
        firmware = self._firmware
        report = firmware.latest
        reason = firmware.shutdown_reason
        if report is None:  # 接続したときに問い合わせるので、ここには来ない
            raise MotionError("ステージの状態をまだ受けていない")
        axes = self._settings.axes
        position = Point3d(
            *(
                axis.to_mm(steps)
                for axis, steps in zip(axes, report.position, strict=True)
            )
        )
        if reason is not None:
            return MotionStatus(
                position=position,
                velocity=Velocity(),
                state=MotionState.SHUTDOWN,
                homed=self._homed,
                shutdown_reason=reason,
            )
        load_cell = firmware.load_cell
        reading = load_cell.reading() if load_cell is not None else None
        return MotionStatus(
            position=position,
            velocity=Velocity(
                *(
                    axis.to_mm(steps)
                    for axis, steps in zip(axes, report.velocity, strict=True)
                )
            ),
            state=MotionState.MOVING if report.state.running else MotionState.IDLE,
            homed=self._homed,
            load=reading.grams if reading is not None else None,
            load_error=reading.error if reading is not None else None,
        )

    @override
    def home(self, axes: Collection[Axis] = ()) -> None:
        with self._lock, self._translate():
            firmware = self._ready()
            firmware.set_enabled(True)
            for axis in homing_order(axes, self._homed):
                single: frozenset[Axis] = frozenset((axis,))
                self._homed = self._homed - single
                home_axis(firmware, axis)
                self._homed = self._homed | single

    @override
    def set_velocity(self, velocity: Velocity) -> None:
        values = (velocity.x, velocity.y, velocity.z)
        with self._lock, self._translate():
            firmware = self._ready()
            moving: set[Axis] = {
                axis for axis, value in zip(AXES, values, strict=True) if value
            }
            self._require_homed(moving)
            if not firmware.streaming:
                if not moving:
                    return
                firmware.set_enabled(True)
                firmware.start()
            steps = [
                axis.to_steps(value)
                for axis, value in zip(self._settings.axes, values, strict=True)
            ]
            firmware.set_velocity((steps[0], steps[1], steps[2]))

    @override
    def stop(self) -> None:
        with self._lock, self._translate():
            firmware = self._ready()
            if firmware.streaming:
                firmware.stop()

    @override
    def move_to(
        self,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        *,
        speed: float | None = None,
    ) -> None:
        if speed is not None and not speed > 0:
            raise ValueError(f"速さは正にする: {speed}")
        if outside := self._limits.outside(x=x, y=y, z=z, feed=speed):
            raise ValueError(f"可動域の外です: {', '.join(outside)}")
        wanted: dict[Axis, float] = {
            axis: value
            for axis, value in zip(AXES, (x, y, z), strict=True)
            if value is not None
        }
        with self._lock, self._translate():
            firmware = self._ready()
            self._require_homed(set(wanted))
            if firmware.streaming:
                firmware.stop()
            firmware.set_enabled(True)
            firmware.travel(
                {
                    axis: firmware.axis(axis).to_steps(value)
                    for axis, value in wanted.items()
                },
                speed,
            )
            firmware.query()

    @override
    def relax(self) -> None:
        with self._lock, self._translate():
            firmware = self._ready()
            if firmware.streaming:
                firmware.stop()
            firmware.set_enabled(False)
            self._homed = frozenset()
            firmware.forget_origin(*AXES)
            firmware.query()

    @override
    def restart(self) -> None:
        with self._lock:
            self._firmware.close()
            self._homed = frozenset()
            # 立て直す間と、立て直せなかった後は、閉じた前の接続の古い状態を返さない
            self._failure = "ファームを立て直している"
            try:
                with self._translate():
                    self._firmware = open_firmware(self._settings, reset=True)
            except MotionError as error:
                self._failure = (
                    f"ファームを立て直せなかった（{error}）。再起動をやり直してください"
                )
                raise
            self._failure = None

    @override
    def emergency_stop(self) -> None:
        # 操作のロックを取らない（原点出しや移動の待ちの最中でもすぐ送る）
        try:
            self._firmware.emergency_stop()
        except McuError as error:
            raise MotionError(f"緊急停止を送れない: {error}") from error

    @override
    def tare(self) -> None:
        # 操作のロックを取らない（ロードセルは受信スレッドが読み、MCU には何も送らない）
        load_cell = self._ready().load_cell
        if load_cell is None:
            raise ValueError(
                "この機体にはロードセルがありません（[pnp.motion.load_cell]）"
            )
        load_cell.tare()

    @override
    def close(self) -> None:
        with self._lock:
            self._firmware.close()

    @contextlib.contextmanager
    def _translate(self) -> Iterator[None]:
        """MCU の例外を :class:`MotionError` に直す。接続が切れたら覚えておく."""
        try:
            yield
        except McuShutdown as error:
            raise MotionError(f"MCU が shutdown した: {error.reason}") from error
        except ProcedureError as error:
            raise MotionError(str(error)) from error
        except McuError as error:
            self._failure = f"MCU との接続が切れた: {error}"
            raise MotionError(self._failure) from error

    def _ready(self) -> PnpMotionFirmware:
        """操作を受け付けるファーム（接続が切れた、shutdown した、なら :class:`MotionError`）."""
        if self._failure is not None:
            raise MotionError(self._failure)
        firmware = self._firmware
        if firmware.shutdown_reason is not None:
            raise MotionError(
                f"MCU が shutdown している: {firmware.shutdown_reason}（ファームの再起動が要る）"
            )
        return firmware

    def _require_homed(self, axes: Collection[Axis]) -> None:
        if unhomed := sorted(set(axes) - self._homed):
            raise ValueError(
                f"原点出しをしていない軸は動かせません: {', '.join(unhomed)}"
            )


def open_motion_stage(settings: McuMotionSettings) -> MotionStage:
    """MCU を開いてステージを作る（テストは conftest の ``forbid_real_motion`` が塞ぐ生成口）.

    Raises:
        MotionError: MCU を開けない、pnp-motion ファームではない、設定できない
    """
    return McuMotionStage(settings)
