"""ピンセットのサーボ（Feetech STS3032）との通信口.

ピンセットは 2 台のサーボで動く。

- Rotation: ピンセットの向き。多回転するのでステップモード（:attr:`ServoMode.STEP`）で相対 step を送る
- Open Close: ピンセットの開閉。位置モード（:attr:`ServoMode.POSITION`）で閉じ〜開きの間の絶対位置を送る

モードはサーボの EEPROM にある。取り付けたばかりのサーボは役割が分からないので、役割を特定したあとに
:meth:`ServoBus.set_mode` で書く（``pcbasm.pnp.servo_setup``）。:class:`Tweezer` は読んで確かめるだけ。
実装は scservo_sdk の :class:`FeetechBus` と、``pcbasm.hal.fake.FakeServoBus`` の 2 つに限る。
"""

from __future__ import annotations

import contextlib
import enum
import math
import termios
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from typing import override

from scservo_sdk import COMM_SUCCESS, PacketHandler, PortHandler

from pcbasm.errors import DeviceError

STEPS_PER_REV = 4096
BROADCAST_ID = 254  # 全サーボ宛て（応答は返らない）

# STS シリーズのコントロールテーブル
_ADDR_MIN_ANGLE_LIMIT = 9  # EEPROM。位置モードの goal はこの範囲に収められる
_ADDR_MAX_ANGLE_LIMIT = 11  # EEPROM
_ADDR_MODE = 33  # EEPROM（書くときは LOCK を外す）
_ADDR_TORQUE_ENABLE = 40
_ADDR_ACC = 41  # ACC から 7 byte: ACC, GOAL_POSITION(42), GOAL_TIME(44), GOAL_SPEED(46)
_ADDR_GOAL_POSITION = 42
_ADDR_LOCK = 55  # EEPROM の書き込みロック（0 で外す、1 で戻す）
_ADDR_PRESENT_POSITION = 56
_ADDR_MOVING = 66
_GOAL_LENGTH = 7

DEFAULT_SPEED = 500  # goal speed の既定値 [step/s]
_MAX_SPEED = (1 << 15) - 1  # goal speed は 2 byte で、bit15 は使わない
DEFAULT_ACCELERATION = 10  # goal の加速度の既定値 [100 step/s²]
_MAX_ACCELERATION = 254

# ステップモードの goal は sign-magnitude（bit15 が負号、下位 15 bit が大きさ）
_STEP_SIGN = 1 << 15
MAX_STEPS = _STEP_SIGN - 1  # 1 回に送れる step の大きさ

# EEPROM の書き込みは応答が遅れることがある（MODE が実際に変わると約 40 ms。SDK が書き込みの応答を待つのは約 34 ms）
_EEPROM_REPLY_TIMEOUT_MS = 200
_PKT_ERROR = 4  # status packet の ERROR の位置（HEADER0 HEADER1 ID LENGTH ERROR ...）

# Rotation は止めるときの行き過ぎを小さくするため速く加減速する（45°/s で減速距離 約 2°）
_ROTATION_ACCELERATION = 50  # [100 step/s²]
# 連続回転で残りを送り足す下限・上限（減速距離に足す、今の速さで進む秒数）
_KEEP_FLOOR_SECONDS = 0.1
_KEEP_CEILING_SECONDS = 0.3

_POLL_INTERVAL = 0.02  # wait_stopped が moving を見る間隔 [sec]
# 指令の直後はまだ moving が立たない（実機で約 0.05 秒）。この間に立たなければ動かない指令とみなす
_START_GRACE = 0.2  # [sec]


class ServoMode(enum.IntEnum):
    """サーボの動作モード（コントロールテーブルの MODE）."""

    POSITION = 0
    STEP = 3


class ServoError(DeviceError):
    """サーボが応答しない・通信に失敗した・モードが役割と合わない."""


class ServoBus(ABC):
    """サーボのバス 1 本（ID でサーボを選ぶ）."""

    @abstractmethod
    def ping(self, servo_id: int) -> bool:
        """その ID のサーボが応答するか.

        Raises:
            ServoError: シリアルの I/O に失敗した場合
        """

    @abstractmethod
    def mode(self, servo_id: int) -> ServoMode:
        """動作モードを読む.

        Raises:
            ServoError: 通信に失敗した、または位置・ステップ以外のモードの場合
        """

    @abstractmethod
    def position(self, servo_id: int) -> int:
        """位置モードのサーボの現在位置を読む（raw 0〜4095）.

        ステップモードでは、同じレジスタが goal までの残りの step を返す（:meth:`remaining_steps`）。

        Raises:
            ServoError: 通信に失敗した場合
        """

    @abstractmethod
    def moving(self, servo_id: int) -> bool:
        """動いている途中か.

        Raises:
            ServoError: 通信に失敗した場合
        """

    @abstractmethod
    def set_torque(self, servo_id: int, on: bool) -> None:
        """トルクを入れる・切る.

        Raises:
            ServoError: 通信に失敗した場合
        """

    @abstractmethod
    def release_all(self) -> None:
        """バスにつながる全サーボのトルクを切る（ID を知らなくても届くブロードキャスト。応答は待たない）.

        Raises:
            ServoError: 送信に失敗した場合
        """

    @abstractmethod
    def angle_limits(self, servo_id: int) -> tuple[int, int]:
        """角度制限（下限、上限。raw）を EEPROM から読む（0 / 0 は制限なし）.

        Raises:
            ServoError: 通信に失敗した場合
        """

    @abstractmethod
    def clear_angle_limits(self, servo_id: int) -> None:
        """角度制限を外す（下限・上限とも 0 を EEPROM に書く）.

        ステップモードでも角度制限は効き、同じ向きに回し続けると制限の端で止まる。多回転させるサーボは
        制限を外す。外したステップモードのサーボでは、:meth:`position` は goal までの残りの step を返す。

        Raises:
            ServoError: 通信に失敗した場合
        """

    @abstractmethod
    def set_angle_limits(self, servo_id: int, low: int, high: int) -> None:
        """位置モードで動ける範囲（raw）を EEPROM に書く（サーボは goal をこの範囲に収める）.

        Raises:
            ValueError: 0 <= low < high <= 4095 でない場合（何も送らない）
            ServoError: 通信に失敗した場合
        """

    @abstractmethod
    def set_mode(self, servo_id: int, mode: ServoMode) -> None:
        """動作モードを EEPROM に書く（電源を切っても残る。トルクは呼ぶ側が先に切る）.

        Raises:
            ServoError: 通信に失敗した場合
        """

    @abstractmethod
    def goal(self, servo_id: int) -> int:
        """位置モードのサーボの goal（最後に送った行き先。raw 0〜4095）を読む.

        Raises:
            ServoError: 通信に失敗した場合
        """

    @abstractmethod
    def goto(self, servo_id: int, position: int, *, speed: int = DEFAULT_SPEED) -> None:
        """位置モードのサーボを絶対位置（raw 0〜4095）へ speed [step/s] で動かす（完了は待たない）.

        Raises:
            ValueError: position が 0〜4095 の外か、speed が 1〜32767 の外の場合（何も送らない）
            ServoError: 通信に失敗した場合
        """

    @abstractmethod
    def step(
        self,
        servo_id: int,
        steps: int,
        *,
        speed: int = DEFAULT_SPEED,
        acceleration: int = DEFAULT_ACCELERATION,
    ) -> None:
        """ステップモードのサーボを相対に steps だけ動かす（負は逆向き。完了は待たない）.

        動いている途中に送ると、goal までの残りに足される（速さは途切れない）。

        Args:
            servo_id: サーボ ID
            steps: 動かす step
            speed: 速さ [step/s]
            acceleration: 加減速 [100 step/s²]

        Raises:
            ValueError: speed が 1〜32767、acceleration が 1〜254 の外の場合（何も送らない）
            ServoError: 通信に失敗した場合
        """

    @abstractmethod
    def remaining_steps(self, servo_id: int) -> int:
        """角度制限を外したステップモードのサーボの、goal までの残りの step（符号つき）.

        Raises:
            ServoError: 通信に失敗した場合
        """

    @abstractmethod
    def close(self) -> None:
        """バスを閉じる."""


class FeetechBus(ServoBus):
    """scservo_sdk で STS シリーズのサーボと話す :class:`ServoBus`（:func:`open_servo_bus`
    で作る）.

    複数のスレッドから呼ばれ得るので、1 本の Lock で送受信を直列にする。シリアルの I/O が失敗したら（URT-2
    を抜いたなど）ポートを閉じ、次の呼び出しで開き直す。
    """

    def __init__(self, port: PortHandler) -> None:
        """開いたポートで初期化する."""
        self._port = port
        self._packet = PacketHandler(0)  # STS 系はリトルエンディアン
        self._lock = threading.Lock()
        self._closed = False

    @override
    def ping(self, servo_id: int) -> bool:
        """その ID のサーボが応答するか."""
        _, result, _ = self._transact(
            servo_id, lambda: self._packet.ping(self._port, servo_id)
        )
        return result == COMM_SUCCESS

    @override
    def mode(self, servo_id: int) -> ServoMode:
        """動作モードを読む."""
        value = self._read1(servo_id, _ADDR_MODE)
        try:
            return ServoMode(value)
        except ValueError:
            raise ServoError(
                f"サーボ {servo_id} のモードが位置（0）でもステップ（3）でもありません: {value}"
            ) from None

    @override
    def position(self, servo_id: int) -> int:
        """現在位置を読む（raw 0〜4095）."""
        return self._read2(servo_id, _ADDR_PRESENT_POSITION)

    @override
    def goal(self, servo_id: int) -> int:
        """GOAL_POSITION を読む."""
        return self._read2(servo_id, _ADDR_GOAL_POSITION)

    @override
    def moving(self, servo_id: int) -> bool:
        """動いている途中か."""
        return self._read1(servo_id, _ADDR_MOVING) != 0

    @override
    def set_torque(self, servo_id: int, on: bool) -> None:
        """トルクを入れる・切る."""
        self._write1(servo_id, _ADDR_TORQUE_ENABLE, int(on))

    @override
    def release_all(self) -> None:
        """ブロードキャストで TORQUE_ENABLE に 0 を書く（サーボは応答しない）."""
        result = self._transact(
            BROADCAST_ID,
            lambda: self._packet.write1ByteTxOnly(
                self._port, BROADCAST_ID, _ADDR_TORQUE_ENABLE, 0
            ),
        )
        self._check(BROADCAST_ID, result, 0)

    @override
    def set_mode(self, servo_id: int, mode: ServoMode) -> None:
        """EEPROM のロックを外して動作モードを書き、ロックを戻す（失敗してもロックは戻す）."""
        self._write_eeprom(servo_id, ((_ADDR_MODE, 1, int(mode)),))

    @override
    def angle_limits(self, servo_id: int) -> tuple[int, int]:
        """MIN / MAX_ANGLE_LIMIT を読む."""
        return (
            self._read2(servo_id, _ADDR_MIN_ANGLE_LIMIT),
            self._read2(servo_id, _ADDR_MAX_ANGLE_LIMIT),
        )

    @override
    def clear_angle_limits(self, servo_id: int) -> None:
        """EEPROM のロックを外して MIN / MAX_ANGLE_LIMIT に 0 を書き、ロックを戻す."""
        self._write_eeprom(
            servo_id, ((_ADDR_MIN_ANGLE_LIMIT, 2, 0), (_ADDR_MAX_ANGLE_LIMIT, 2, 0))
        )

    @override
    def set_angle_limits(self, servo_id: int, low: int, high: int) -> None:
        """EEPROM のロックを外して MIN / MAX_ANGLE_LIMIT を書き、ロックを戻す."""
        check_angle_limits(low, high)
        self._write_eeprom(
            servo_id,
            ((_ADDR_MIN_ANGLE_LIMIT, 2, low), (_ADDR_MAX_ANGLE_LIMIT, 2, high)),
        )

    def _write_eeprom(
        self, servo_id: int, writes: tuple[tuple[int, int, int], ...]
    ) -> None:
        """ロックを外して（アドレス、バイト数、値）を順に書き、ロックを戻す（失敗してもロックは戻す）."""
        self._write1(servo_id, _ADDR_LOCK, 0)
        try:
            for address, length, value in writes:
                self._write_slow(servo_id, address, length, value)
        finally:
            self._write1(servo_id, _ADDR_LOCK, 1)

    @override
    def goto(self, servo_id: int, position: int, *, speed: int = DEFAULT_SPEED) -> None:
        """位置モードのサーボを絶対位置へ動かす.

        Raises:
            ValueError: position が 0〜4095 の外か、speed が 1〜32767 の外の場合
        """
        if not 0 <= position < STEPS_PER_REV:
            raise ValueError(
                f"位置は 0〜{STEPS_PER_REV - 1} の範囲で送ります: {position}"
            )
        check_speed(speed)
        self._write_goal(servo_id, position, speed, DEFAULT_ACCELERATION)

    @override
    def step(
        self,
        servo_id: int,
        steps: int,
        *,
        speed: int = DEFAULT_SPEED,
        acceleration: int = DEFAULT_ACCELERATION,
    ) -> None:
        """ステップモードのサーボを相対に steps だけ動かす.

        Raises:
            ValueError: |steps| が 32767 を超えるか、speed か acceleration が範囲の外の場合
        """
        if abs(steps) > MAX_STEPS:
            raise ValueError(f"1 回に送れる step は ±{MAX_STEPS} までです: {steps}")
        check_speed(speed)
        check_acceleration(acceleration)
        self._write_goal(servo_id, _to_sign_magnitude(steps), speed, acceleration)

    @override
    def remaining_steps(self, servo_id: int) -> int:
        """PRESENT_POSITION（制限を外したステップモードでは残りの step）を符号つきで読む."""
        return _from_sign_magnitude(self._read2(servo_id, _ADDR_PRESENT_POSITION))

    @override
    def close(self) -> None:
        """ポートを閉じる."""
        with self._lock:
            self._closed = True
            if self._port.is_open:
                self._port.closePort()

    def _read2(self, servo_id: int, address: int) -> int:
        value, result, error = self._transact(
            servo_id,
            lambda: self._packet.read2ByteTxRx(self._port, servo_id, address),
        )
        self._check(servo_id, result, error)
        return value

    def _read1(self, servo_id: int, address: int) -> int:
        value, result, error = self._transact(
            servo_id,
            lambda: self._packet.read1ByteTxRx(self._port, servo_id, address),
        )
        self._check(servo_id, result, error)
        return value

    def _write1(self, servo_id: int, address: int, value: int) -> None:
        result, error = self._transact(
            servo_id,
            lambda: self._packet.write1ByteTxRx(self._port, servo_id, address, value),
        )
        self._check(servo_id, result, error)

    def _write_slow(self, servo_id: int, address: int, length: int, value: int) -> None:
        """EEPROM を書き、遅れて届く応答を長めに待つ（SDK の待ち時間で切ると、遅れた応答が次の読み出しに混ざる）."""

        def call() -> tuple[list[int], int]:
            data = [(value >> (8 * i)) & 0xFF for i in range(length)]
            result = self._packet.writeTxOnly(
                self._port, servo_id, address, length, data
            )
            if result != COMM_SUCCESS:
                return [], result
            self._port.setPacketTimeoutMillis(_EEPROM_REPLY_TIMEOUT_MS)
            return self._packet.rxPacket(self._port)

        packet, result = self._transact(servo_id, call)
        self._check(servo_id, result, packet[_PKT_ERROR] if packet else 0)

    def _write_goal(
        self, servo_id: int, goal: int, speed: int, acceleration: int
    ) -> None:
        data = [
            acceleration,
            goal & 0xFF,
            (goal >> 8) & 0xFF,
            0,
            0,
            speed & 0xFF,
            speed >> 8,
        ]
        result, error = self._transact(
            servo_id,
            lambda: self._packet.writeTxRx(
                self._port, servo_id, _ADDR_ACC, _GOAL_LENGTH, data
            ),
        )
        self._check(servo_id, result, error)

    def _transact[T](self, servo_id: int, call: Callable[[], T]) -> T:
        """Lock の下で SDK を 1 回呼ぶ（I/O の失敗は ServoError にし、次の呼び出しで開き直す）."""
        with self._lock:
            if self._closed:
                raise ServoError(f"サーボのバスは閉じています（サーボ {servo_id}）")
            try:
                if not self._port.is_open:
                    self._reopen()
                return call()
            # serial.SerialException は OSError の派生。flush（tcdrain）は termios.error を出す
            except (OSError, termios.error) as exc:
                # SDK は送受信の途中で例外が出ると is_using を戻さず、以後 COMM_PORT_BUSY になる
                self._port.is_using = False
                with contextlib.suppress(OSError, termios.error):
                    self._port.closePort()
                raise ServoError(
                    f"サーボ {servo_id} と通信できません（{self._port.getPortName()}）: {exc}"
                ) from exc

    def _reopen(self) -> None:
        if not self._port.setBaudRate(self._port.getBaudRate()):
            raise ServoError(
                f"サーボのポート {self._port.getPortName()} を開き直せません"
            )

    def _check(self, servo_id: int, result: int, error: int) -> None:
        if result != COMM_SUCCESS:
            raise ServoError(
                f"サーボ {servo_id} と通信できません: {self._packet.getTxRxResult(result)}"
            )
        if error != 0:
            raise ServoError(
                f"サーボ {servo_id} がエラーを返しました: {self._packet.getRxPacketError(error)}"
            )


def open_servo_bus(port: str, baudrate: int = 1_000_000) -> FeetechBus:
    """シリアルポートを開いてサーボのバスを作る.

    Raises:
        ServoError: ポートを開けない・その baudrate を使えない場合
    """
    handler = PortHandler(port)
    try:
        opened = handler.setBaudRate(baudrate)
    except OSError as exc:  # serial.SerialException は OSError の派生
        raise ServoError(f"サーボのポート {port} を開けません: {exc}") from exc
    if not opened:
        raise ServoError(f"サーボのポート {port} を {baudrate} bps で開けません")
    return FeetechBus(handler)


def check_angle_limits(low: int, high: int) -> None:
    """位置モードの角度制限（raw）の組を確かめる.

    Raises:
        ValueError: 0 <= low < high <= 4095 でない場合
    """
    if not 0 <= low < high < STEPS_PER_REV:
        raise ValueError(
            f"角度制限は 0 <= 下限 < 上限 <= {STEPS_PER_REV - 1} で送ります: {low}, {high}"
        )


def _to_sign_magnitude(value: int) -> int:
    return value if value >= 0 else _STEP_SIGN | -value


def _from_sign_magnitude(raw: int) -> int:
    return -(raw & MAX_STEPS) if raw & _STEP_SIGN else raw


def check_acceleration(acceleration: int) -> None:
    """Goal の加速度 [100 step/s²] が送れる値か確かめる.

    Raises:
        ValueError: 1〜254 の外の場合
    """
    if not 0 < acceleration <= _MAX_ACCELERATION:
        raise ValueError(
            f"加速度は 1〜{_MAX_ACCELERATION}（100 step/s² 単位）で送ります: {acceleration}"
        )


def check_speed(speed: int) -> None:
    """Goal speed [step/s] が送れる値か確かめる.

    Raises:
        ValueError: 1〜32767 の外の場合
    """
    if not 0 < speed <= _MAX_SPEED:
        raise ValueError(f"速度は 1〜{_MAX_SPEED} step/s で送ります: {speed}")


def _degrees_per_second_to_speed(degrees_per_second: float) -> int:
    """角速度 [deg/s] を goal speed [step/s] にする（四捨五入、最低 1）."""
    return max(1, round(degrees_per_second * STEPS_PER_REV / 360))


def position_to_degrees(position: int) -> float:
    """位置（raw 0〜4095）をサーボの絶対角 [deg]（0〜360）にする."""
    return position * 360 / STEPS_PER_REV


def degrees_to_position(degrees: float) -> int:
    """サーボの絶対角 [deg]（0〜360）を最も近い位置（raw 0〜4095）にする."""
    return round(degrees * STEPS_PER_REV / 360) % STEPS_PER_REV


def degrees_to_steps(degrees: float) -> int:
    """角度 [deg] を step に換算する（四捨五入。端数 0.5 は 0 から遠い方へ）."""
    steps = degrees * STEPS_PER_REV / 360
    return int(math.copysign(math.floor(abs(steps) + 0.5), steps))


def scan(bus: ServoBus, ids: Iterable[int]) -> tuple[int, ...]:
    """Ping に応答した ID を ids の順で返す."""
    return tuple(servo_id for servo_id in ids if bus.ping(servo_id))


def wiggle(
    bus: ServoBus, servo_id: int, degrees: float = 5.0, *, timeout: float = 2.0
) -> None:
    """サーボを +degrees → -degrees → 元の位置と動かす（どのサーボがどの役割か人が見て判別する）.

    トルクを入れてから動かし、各移動の完了を待つ。位置モードでは :func:`hold_position` で
    トルクを入れる。移動が timeout 内に終わらなければ、当たりに押し続けないようトルクを切る。

    Args:
        bus: サーボのバス
        servo_id: 動かすサーボ
        degrees: 振る角度 [deg]
        timeout: 1 回の移動の完了を待つ秒数

    Raises:
        ServoError: 応答しない、移動が timeout 内に終わらない、
            または位置モードで元の位置 ±degrees が 0〜4095 を外れる場合（このときは動かさない）
    """
    if not bus.ping(servo_id):
        raise ServoError(f"サーボ {servo_id} が応答しません")
    n = degrees_to_steps(degrees)
    if bus.mode(servo_id) is ServoMode.STEP:
        bus.set_torque(servo_id, True)
        for steps in (n, -2 * n, n):
            bus.step(servo_id, steps)
            _wait_or_release(bus, servo_id, timeout)
        return
    origin = bus.position(servo_id)
    targets = (origin + n, origin - n, origin)
    if not all(0 <= target < STEPS_PER_REV for target in targets):
        raise ServoError(
            f"サーボ {servo_id} は位置 {origin} から ±{n} step 動かすと 0〜{STEPS_PER_REV - 1} を外れます"
        )
    hold_position(bus, servo_id)
    for target in targets:
        bus.goto(servo_id, target)
        _wait_or_release(bus, servo_id, timeout)


def hold_position(bus: ServoBus, servo_id: int) -> int:
    """位置モードのサーボに今の位置でトルクを入れ、その位置を返す.

    トルクを入れる前に goal を今の位置にし、入れた瞬間に古い goal へ跳ばないようにする。

    Raises:
        ServoError: 通信に失敗した場合
    """
    position = bus.position(servo_id)
    bus.goto(servo_id, position)
    bus.set_torque(servo_id, True)
    return position


def wait_stopped(bus: ServoBus, servo_id: int, *, timeout: float) -> bool:
    """指令を送った直後に呼び、サーボが動き出して止まるまで待つ.

    指令の直後はまだ moving が立たないので、まず動き出すのを少し待つ。その間に動き出さなければ
    （今の位置と同じ所への指令など）、止まっているものとして返す。

    Returns:
        timeout 秒以内に止まったか

    Raises:
        ServoError: 通信に失敗した場合
    """
    start = time.monotonic()
    deadline = start + timeout
    while not bus.moving(servo_id):
        if time.monotonic() > start + _START_GRACE:
            return True
        time.sleep(_POLL_INTERVAL)
    while bus.moving(servo_id):
        if time.monotonic() > deadline:
            return False
        time.sleep(_POLL_INTERVAL)
    return True


def _wait_or_release(bus: ServoBus, servo_id: int, timeout: float) -> None:
    if not wait_stopped(bus, servo_id, timeout=timeout):
        bus.set_torque(servo_id, False)
        raise ServoError(
            f"サーボ {servo_id} の移動が {timeout} 秒で終わらないので、トルクを切りました"
        )


class Tweezer:
    """ピンセット（Rotation と Open Close の 2 台のサーボ）.

    指令を送るだけで、動き終わるのは待たない。Open Close の角度から開き幅への換算（角度 → 直動 →
    開き）はモデル化せず、角度のまま扱う。

    トルクは組んだときではなく、指令ごとに動かすサーボだけに入れる。今の開き具合を読むだけなら、
    脱力したサーボを保持しない。Rotation を回しても Open Close の goal には触れないので、
    つまんだまま向きを変えても把持力は抜けない。
    """

    def __init__(
        self,
        bus: ServoBus,
        *,
        rotation_id: int,
        open_close_id: int,
        closed_position: int,
        open_position: int,
        rotation_speed: float,
    ) -> None:
        """両方のサーボの応答・モード・角度制限を確かめる.

        角度制限はサーボ設定ジョブが書いた状態であることを確かめる。Rotation は制限なし（0 / 0。
        制限があると回し続けたときに端で止まる）、Open Close は閉じ切り〜開き切り（開き過ぎて
        逆作動式の機構を壊さない守り）。

        Args:
            bus: サーボのバス
            rotation_id: Rotation（ステップモード）のサーボ ID
            open_close_id: Open Close（位置モード）のサーボ ID
            closed_position: Open Close の閉じた位置（raw 0〜4095）
            open_position: Open Close の開いた位置（raw 0〜4095）
            rotation_speed: Rotation を回す速さ [deg/s]

        Raises:
            ServoError: どちらかが応答しないか、モードか角度制限が役割と合わない場合
        """
        roles = (
            ("rotation", rotation_id, ServoMode.STEP),
            ("open_close", open_close_id, ServoMode.POSITION),
        )
        for role, servo_id, _ in roles:
            if not bus.ping(servo_id):
                raise ServoError(f"{role} のサーボ {servo_id} が応答しません")
        for role, servo_id, expected in roles:
            if (mode := bus.mode(servo_id)) is not expected:
                raise ServoError(
                    f"{role} のサーボ {servo_id} のモードが {expected.name} ではありません: {mode.name}"
                )
        limits = (
            ("rotation", rotation_id, (0, 0), "制限なし（0 / 0）"),
            (
                "open_close",
                open_close_id,
                (
                    min(closed_position, open_position),
                    max(closed_position, open_position),
                ),
                "閉じ切り〜開き切り",
            ),
        )
        for role, servo_id, expected_limits, meaning in limits:
            if (actual := bus.angle_limits(servo_id)) != expected_limits:
                raise ServoError(
                    f"{role} のサーボ {servo_id} の角度制限が {actual[0]}〜{actual[1]} です"
                    f"（{meaning}の {expected_limits[0]}〜{expected_limits[1]} が要ります）。"
                    "サーボ設定ジョブをやり直してください"
                )
        self._bus = bus
        self._rotation_id = rotation_id
        self._open_close_id = open_close_id
        self._closed = closed_position
        self._open = open_position
        self._direction = 1 if open_position > closed_position else -1
        self._rotation_speed = _degrees_per_second_to_speed(rotation_speed)
        # 減速距離 v² / 2a（切り上げ）。止めるときはこれだけ残し、行き過ぎて戻らないようにする
        self._stopping_steps = math.ceil(
            self._rotation_speed**2 / (2 * _ROTATION_ACCELERATION * 100)
        )
        self._keep_floor = self._stopping_steps + int(
            self._rotation_speed * _KEEP_FLOOR_SECONDS
        )
        self._keep_ceiling = self._stopping_steps + int(
            self._rotation_speed * _KEEP_CEILING_SECONDS
        )

    def rotate_by(self, degrees: float) -> None:
        """向きを degrees だけ回す（0 step に丸まれば何も送らない）."""
        steps = degrees_to_steps(degrees)
        if steps != 0:
            self._step_rotation(steps)

    def keep_rotating(self, direction: int) -> None:
        """Direction（+1 か -1）の向きへ回り続ける（押している間、周期的に呼ぶ）.

        goal までの残りが少なくなったら、同じ向きに送り足す（step は残りに足されるので途切れない）。
        残りには上限があるので、呼ばれなくなれば短い間（今の速さで 0.3 秒と減速の分）で止まる。

        Raises:
            ValueError: direction が +1 / -1 でない場合（何も送らない）
        """
        if direction not in (1, -1):
            raise ValueError(f"回る向きは +1 か -1 で指定します: {direction}")
        remaining = self._bus.remaining_steps(self._rotation_id)
        if direction * remaining < self._keep_floor:
            self._step_rotation(direction * self._keep_ceiling - remaining)

    def stop_rotation(self) -> None:
        """回転を止める（残りを減速距離だけにする。残りを 0 にすると行き過ぎて戻る）."""
        remaining = self._bus.remaining_steps(self._rotation_id)
        if abs(remaining) > self._stopping_steps:
            stop_at = int(math.copysign(self._stopping_steps, remaining))
            self._step_rotation(stop_at - remaining)

    def _step_rotation(self, steps: int) -> None:
        # パネルで大きく回している途中に逆向きを押すと、1 回に送れる step を超え得る。
        # 収めて送れば、残りは次の周期の送り足しで埋まる
        steps = max(-MAX_STEPS, min(steps, MAX_STEPS))
        self._bus.set_torque(self._rotation_id, True)
        self._bus.step(
            self._rotation_id,
            steps,
            speed=self._rotation_speed,
            acceleration=_ROTATION_ACCELERATION,
        )

    def open_by(self, degrees: float) -> None:
        """開く向きを正として、Open Close を今の位置から degrees だけ動かす.

        Raises:
            ValueError: 行き先が閉じ〜開きの範囲を外れる場合（何も送らない）
        """
        target = self._bus.position(
            self._open_close_id
        ) + self._direction * degrees_to_steps(degrees)
        low, high = sorted((self._closed, self._open))
        if not low <= target <= high:
            raise ValueError(
                f"開閉の行き先 {target} が閉じ（{self._closed}）〜開き（{self._open}）の外です"
            )
        self._goto_open_close(target)

    def open_to(self, fraction: float) -> None:
        """Open Close を開き具合 fraction（0=閉じ切り、1=開き切り）の位置へ動かす.

        Raises:
            ValueError: fraction が 0〜1 の外の場合（何も送らない）
        """
        if not 0.0 <= fraction <= 1.0:
            raise ValueError(f"開き具合は 0〜1 で指定します: {fraction}")
        self._goto_open_close(
            round(self._closed + (self._open - self._closed) * fraction)
        )

    def open_fraction(self) -> float:
        """今の開き具合（0=閉じ切り、1=開き切り。範囲の外にいればその値のまま）."""
        return self._fraction(self._bus.position(self._open_close_id))

    def target_fraction(self) -> float:
        """Open Close の goal（最後に送った行き先）の開き具合.

        つまんで閉じ切りの手前で止まっていても、送った開き具合を返す。
        """
        return self._fraction(self._bus.goal(self._open_close_id))

    def _fraction(self, position: int) -> float:
        return (position - self._closed) / (self._open - self._closed)

    def _goto_open_close(self, target: int) -> None:
        # goal を先に書くので、トルクを入れた瞬間に古い goal へ跳ばない
        self._bus.goto(self._open_close_id, target)
        self._bus.set_torque(self._open_close_id, True)

    def opening(self) -> float:
        """閉じた位置から開く向きへの角度 [deg]."""
        position = self._bus.position(self._open_close_id)
        return (position - self._closed) * self._direction * 360 / STEPS_PER_REV
