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
- **原点出しの後:** MCU は ``contact`` のまま残るが、止まっている（``IDLE``）として扱う。
  速度指令を受けたら ``velocity_stream_start`` で開始し直す
- **接触停止（ロードセルがある機体）:** 動かし始めるたびに（原点出し、座標指定の移動、ジョグ、速度指令）、
  :meth:`PnpMotionFirmware.start` でロードセルの接触停止を張る。MCU が自分で止める（ファームの docstring の
  「接触停止」）。風袋がまだか値が来ていなければ動かさない（:class:`MotionError`）
- **接触保持:** 止まったことには、次の操作の最初（ロックの中）で気づき、保持に入る。保持に入るときに、
  MCU の可動域を X・Y は今の位置の ±1 step、Z は接触した位置より押す側へ行けない範囲に縮める（ホストが指令を
  捨てるのと二重に守る。±1 step は corexy で位置が 1 step ずれて見える分と、``set_limits`` が要る下限 < 上限の
  ため）。Z が端にあって空になるなら 1 step 内側に寄せる。保持の間は接触停止を張らない（荷重がしきい値の
  近くにあり、張るとすぐに引かれる）。速度指令は :func:`~pcbasm.hal.motion.contact_velocity` で、座標指定の
  移動は :func:`~pcbasm.hal.motion.contact_move_error` で絞り、原点出しと風袋引きは受け付けない
- **保持から戻る:** 保持に入ってから後の荷重が ``release_force`` を 100 ms 下回り続けたら、次の操作の最初で、
  流していれば止めてから可動域を戻す。次に動かすときに接触停止を張り直す。判定を次の操作まで遅らせるのは、
  ロックの外のスレッドから ``set_limits`` を送ると操作の順が崩れるため。:meth:`~McuMotionStage.status` の
  ``contact`` は、保持の間で荷重がまだ下回っていなければ、または MCU が接触で止めてまだ保持に入っていなければ真
- **原点出しの途中の接触:** その軸は「未」のまま保持に入り、:class:`~pcbasm.hal.motion.ContactStopped`。Z を先に
  出すので、X・Y の途中なら Z+（離す向き）で離せる。Z の途中なら Z は「未」で動かせないので、脱力で抜ける
- **ロードセルの異常:** 動かしている間に MCU が異常で止めたら、その操作か次の操作が :class:`MotionError` になる
  （保持には入らない。次に動かすときに張り直す）
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
import logging
import threading
import time
from collections.abc import Collection, Iterator
from typing import override

import attrs

from pcbasm.geometry import Point3d
from pcbasm.hal.mcu.errors import McuError, McuShutdown
from pcbasm.hal.mcu.firmware import (
    AXES,
    ContactStop,
    PnpMotionFirmware,
    ProcedureError,
    open_firmware,
)
from pcbasm.hal.mcu.homing import home_axis
from pcbasm.hal.mcu.settings import McuMotionSettings
from pcbasm.hal.motion import (
    Axis,
    ContactStopped,
    MotionError,
    MotionStage,
    MotionState,
    MotionStatus,
    Velocity,
    contact_move_error,
    contact_velocity,
    homing_order,
)
from pcbasm.hal.stage import Limits, ScalarLimits

_logger = logging.getLogger(__name__)


@attrs.frozen
class _Hold:
    """接触保持（モジュールの docstring）.

    Attributes:
        bounds: 保持に入る前の 3 軸の可動域 [steps]
        since: 保持に入った時刻（``time.monotonic()``）
    """

    bounds: dict[Axis, tuple[int, int]]
    since: float


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
        self._hold: _Hold | None = None
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
        hold = self._hold
        contact = firmware.is_contact(firmware.trigger_reason) or (
            hold is not None
            and load_cell is not None
            and not load_cell.released(hold.since)
        )
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
            contact=contact,
        )

    @override
    def home(self, axes: Collection[Axis] = ()) -> None:
        with self._lock, self._translate():
            firmware = self._ready()
            self._settle(firmware)
            if self._hold is not None:
                raise ValueError(
                    f"接触中は原点出しできません。{self._release_direction()}で離してください"
                )
            firmware.set_enabled(True)
            for axis in homing_order(axes, self._homed):
                single: frozenset[Axis] = frozenset((axis,))
                self._homed = self._homed - single
                try:
                    home_axis(firmware, axis, contact=self._contact(firmware))
                except ContactStop as stop:
                    self._enter_hold(firmware)
                    raise ContactStopped(
                        f"原点出しの途中で接触して止まった（{axis.upper()}）"
                    ) from stop
                self._homed = self._homed | single

    @override
    def set_velocity(self, velocity: Velocity) -> None:
        with self._lock, self._translate():
            firmware = self._ready()
            self._settle(firmware)
            if self._hold is not None:
                velocity = contact_velocity(
                    velocity, press_positive=self._press_positive
                )
            values = (velocity.x, velocity.y, velocity.z)
            moving: set[Axis] = {
                axis for axis, value in zip(AXES, values, strict=True) if value
            }
            self._require_homed(moving)
            if not firmware.streaming:
                if not moving:
                    return
                firmware.set_enabled(True)
                firmware.start(contact=self._contact(firmware))
            steps = [
                axis.to_steps(value)
                for axis, value in zip(self._settings.axes, values, strict=True)
            ]
            firmware.set_velocity((steps[0], steps[1], steps[2]))

    @override
    def stop(self) -> None:
        with self._lock, self._translate():
            firmware = self._ready()
            self._settle(firmware)
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
            self._settle(firmware)
            self._require_homed(set(wanted))
            if self._hold is not None and (
                reason := contact_move_error(
                    self._position(firmware),
                    x,
                    y,
                    z,
                    press_positive=self._press_positive,
                )
            ):
                raise ValueError(reason)
            if firmware.streaming:
                firmware.stop()
            firmware.set_enabled(True)
            try:
                firmware.travel(
                    {
                        axis: firmware.axis(axis).to_steps(value)
                        for axis, value in wanted.items()
                    },
                    speed,
                    contact=self._contact(firmware),
                )
            except ContactStop as stop:
                self._enter_hold(firmware)
                raise ContactStopped("移動の途中で接触して止まった") from stop
            firmware.query()

    @override
    def relax(self) -> None:
        with self._lock, self._translate():
            firmware = self._ready()
            if firmware.streaming:
                firmware.stop()
            # 接触保持も、まだ気づいていない接触や異常も捨てる（可動域は下で広げ直す）
            firmware.clear_trigger()
            self._drop_hold(firmware)
            firmware.set_enabled(False)
            self._homed = frozenset()
            firmware.forget_origin(*AXES)
            firmware.query()

    @override
    def restart(self) -> None:
        with self._lock:
            self._firmware.close()
            self._homed = frozenset()
            self._hold = None
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
        # 操作のロックを取らない（ロードセルは受信スレッドが読む。MCU へは風袋の offset を送るだけ）
        firmware = self._ready()
        load_cell = firmware.load_cell
        if load_cell is None:
            raise ValueError(
                "この機体にはロードセルがありません（[pnp.motion.load_cell]）"
            )
        if self._hold is not None or firmware.is_contact(firmware.trigger_reason):
            raise ValueError(
                f"接触中は風袋引きできません。{self._release_direction()}で離してください"
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

    @property
    def _press_positive(self) -> bool:
        load_cell = self._settings.load_cell
        return load_cell is not None and load_cell.contact.press_positive

    def _release_direction(self) -> str:
        """離す向き（表示用）."""
        if self._press_positive:
            return "Z−"
        return "Z+（ステージを下げる向き）"

    def _position(self, firmware: PnpMotionFirmware) -> Point3d:
        """今の位置 [mm]（問い合わせる）."""
        steps = firmware.query().position
        return Point3d(
            *(
                axis.to_mm(value)
                for axis, value in zip(self._settings.axes, steps, strict=True)
            )
        )

    def _contact(self, firmware: PnpMotionFirmware) -> bool:
        """動かし始めるときに接触停止を張るか（ロードセルがあり、保持の間でない）."""
        return firmware.load_cell is not None and self._hold is None

    def _settle(self, firmware: PnpMotionFirmware) -> None:
        """MCU が接触か異常で止めていれば保持に入るか異常を知らせ、保持から戻れるなら戻す（ロックの中で）.

        Raises:
            MotionError: ロードセルの異常で止まっていた
        """
        reason = firmware.settle_trigger()
        if firmware.is_contact(reason):
            self._enter_hold(firmware)
        elif (error := firmware.sensor_error(reason)) is not None:
            firmware.clear_trigger()
            raise MotionError(f"ロードセルの異常で止めた（{error}）")
        hold = self._hold
        load_cell = firmware.load_cell
        if (
            hold is not None
            and load_cell is not None
            and load_cell.released(hold.since)
        ):
            self._release_hold(firmware, hold)

    def _enter_hold(self, firmware: PnpMotionFirmware) -> None:
        """接触保持に入る: 張ったものを外し、可動域を縮める（MCU は ``contact`` で止まっている）."""
        firmware.clear_trigger()
        load_cell = firmware.load_cell
        if load_cell is not None:
            load_cell.pause_auto_tare(True)
        if self._hold is not None:
            return
        here = firmware.query().position
        saved: dict[Axis, tuple[int, int]] = {
            axis: firmware.bounds(axis) for axis in AXES
        }
        for axis in ("x", "y"):
            position = here[AXES.index(axis)]
            firmware.set_limits(axis, position - 1, position + 1)
        z = here[AXES.index("z")]
        low, high = saved["z"]
        if self._press_positive:
            high = max(z, low + 1)
        else:
            low = min(z, high - 1)
        firmware.set_limits("z", low, high)
        self._hold = _Hold(saved, time.monotonic())
        _logger.info(
            "ロードセルの接触で止めた（位置 %s steps）。荷重が下がるまで %s だけ動かせる",
            here,
            self._release_direction(),
        )

    def _release_hold(self, firmware: PnpMotionFirmware, hold: _Hold) -> None:
        """接触保持から戻る: 流していれば止め、可動域を戻す."""
        if firmware.streaming:
            firmware.stop()
        self._drop_hold(firmware)
        for axis, (low, high) in hold.bounds.items():
            firmware.set_limits(axis, low, high)
        _logger.info("ロードセルの荷重が下がったので、接触保持から戻した")

    def _drop_hold(self, firmware: PnpMotionFirmware) -> None:
        """保持を忘れ、自動の風袋引きを再び許す（可動域は戻さない）."""
        self._hold = None
        if firmware.load_cell is not None:
            firmware.load_cell.pause_auto_tare(False)

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
