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
from collections.abc import Callable, Iterable
from typing import Protocol

from scservo_sdk import COMM_SUCCESS, PacketHandler, PortHandler

from pcbasm.errors import DeviceError

STEPS_PER_REV = 4096
BROADCAST_ID = 254  # 全サーボ宛て（応答は返らない）

# STS シリーズのコントロールテーブル
_ADDR_MODE = 33  # EEPROM（書くときは LOCK を外す）
_ADDR_TORQUE_ENABLE = 40
_ADDR_ACC = 41  # ACC から 7 byte: ACC, GOAL_POSITION(42), GOAL_TIME(44), GOAL_SPEED(46)
_ADDR_LOCK = 55  # EEPROM の書き込みロック（0 で外す、1 で戻す）
_ADDR_PRESENT_POSITION = 56
_ADDR_MOVING = 66
_GOAL_LENGTH = 7

_SPEED = 500  # [step/s]
_ACC = 10  # [100 step/s²]

# ステップモードの goal は sign-magnitude（bit15 が負号、下位 15 bit が大きさ）
_STEP_SIGN = 1 << 15
_MAX_STEPS = _STEP_SIGN - 1

# MODE が実際に変わる書き込みは、応答まで約 40 ms かかる（SDK が書き込みの応答を待つのは約 34 ms）
_MODE_REPLY_TIMEOUT_MS = 200
_PKT_ERROR = 4  # status packet の ERROR の位置（HEADER0 HEADER1 ID LENGTH ERROR ...）

_POLL_INTERVAL = 0.02  # wait_stopped が moving を見る間隔 [sec]
# 指令の直後はまだ moving が立たない（実機で約 0.05 秒）。この間に立たなければ動かない指令とみなす
_START_GRACE = 0.2  # [sec]


class ServoMode(enum.IntEnum):
    """サーボの動作モード（コントロールテーブルの MODE）."""

    POSITION = 0
    STEP = 3


class ServoError(DeviceError):
    """サーボが応答しない・通信に失敗した・モードが役割と合わない."""


class ServoBus(Protocol):
    """サーボのバス 1 本（ID でサーボを選ぶ）."""

    def ping(self, servo_id: int) -> bool:
        """その ID のサーボが応答するか.

        Raises:
            ServoError: シリアルの I/O に失敗した場合
        """
        ...

    def mode(self, servo_id: int) -> ServoMode:
        """動作モードを読む.

        Raises:
            ServoError: 通信に失敗した、または位置・ステップ以外のモードの場合
        """
        ...

    def position(self, servo_id: int) -> int:
        """現在位置を読む（raw 0〜4095）.

        Raises:
            ServoError: 通信に失敗した場合
        """
        ...

    def moving(self, servo_id: int) -> bool:
        """動いている途中か.

        Raises:
            ServoError: 通信に失敗した場合
        """
        ...

    def set_torque(self, servo_id: int, on: bool) -> None:
        """トルクを入れる・切る.

        Raises:
            ServoError: 通信に失敗した場合
        """
        ...

    def release_all(self) -> None:
        """バスにつながる全サーボのトルクを切る（ID を知らなくても届くブロードキャスト。応答は待たない）.

        Raises:
            ServoError: 送信に失敗した場合
        """
        ...

    def set_mode(self, servo_id: int, mode: ServoMode) -> None:
        """動作モードを EEPROM に書く（電源を切っても残る。トルクは呼ぶ側が先に切る）.

        Raises:
            ServoError: 通信に失敗した場合
        """
        ...

    def goto(self, servo_id: int, position: int) -> None:
        """位置モードのサーボを絶対位置（raw 0〜4095）へ動かす（完了は待たない）.

        Raises:
            ValueError: position が 0〜4095 の外の場合
            ServoError: 通信に失敗した場合
        """
        ...

    def step(self, servo_id: int, steps: int) -> None:
        """ステップモードのサーボを相対に steps だけ動かす（負は逆向き。完了は待たない）.

        Raises:
            ServoError: 通信に失敗した場合
        """
        ...

    def close(self) -> None:
        """バスを閉じる."""
        ...


class FeetechBus:
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

    def ping(self, servo_id: int) -> bool:
        """その ID のサーボが応答するか."""
        _, result, _ = self._transact(
            servo_id, lambda: self._packet.ping(self._port, servo_id)
        )
        return result == COMM_SUCCESS

    def mode(self, servo_id: int) -> ServoMode:
        """動作モードを読む."""
        value = self._read1(servo_id, _ADDR_MODE)
        try:
            return ServoMode(value)
        except ValueError:
            raise ServoError(
                f"サーボ {servo_id} のモードが位置（0）でもステップ（3）でもありません: {value}"
            ) from None

    def position(self, servo_id: int) -> int:
        """現在位置を読む（raw 0〜4095）."""
        value, result, error = self._transact(
            servo_id,
            lambda: self._packet.read2ByteTxRx(
                self._port, servo_id, _ADDR_PRESENT_POSITION
            ),
        )
        self._check(servo_id, result, error)
        return value

    def moving(self, servo_id: int) -> bool:
        """動いている途中か."""
        return self._read1(servo_id, _ADDR_MOVING) != 0

    def set_torque(self, servo_id: int, on: bool) -> None:
        """トルクを入れる・切る."""
        self._write1(servo_id, _ADDR_TORQUE_ENABLE, int(on))

    def release_all(self) -> None:
        """ブロードキャストで TORQUE_ENABLE に 0 を書く（サーボは応答しない）."""
        result = self._transact(
            BROADCAST_ID,
            lambda: self._packet.write1ByteTxOnly(
                self._port, BROADCAST_ID, _ADDR_TORQUE_ENABLE, 0
            ),
        )
        self._check(BROADCAST_ID, result, 0)

    def set_mode(self, servo_id: int, mode: ServoMode) -> None:
        """EEPROM のロックを外して動作モードを書き、ロックを戻す（失敗してもロックは戻す）."""
        self._write1(servo_id, _ADDR_LOCK, 0)
        try:
            self._write_mode(servo_id, mode)
        finally:
            self._write1(servo_id, _ADDR_LOCK, 1)

    def goto(self, servo_id: int, position: int) -> None:
        """位置モードのサーボを絶対位置へ動かす.

        Raises:
            ValueError: position が 0〜4095 の外の場合
        """
        if not 0 <= position < STEPS_PER_REV:
            raise ValueError(
                f"位置は 0〜{STEPS_PER_REV - 1} の範囲で送ります: {position}"
            )
        self._write_goal(servo_id, position)

    def step(self, servo_id: int, steps: int) -> None:
        """ステップモードのサーボを相対に steps だけ動かす.

        Raises:
            ValueError: |steps| が 32767 を超える場合
        """
        if abs(steps) > _MAX_STEPS:
            raise ValueError(f"1 回に送れる step は ±{_MAX_STEPS} までです: {steps}")
        self._write_goal(servo_id, steps if steps >= 0 else _STEP_SIGN | -steps)

    def close(self) -> None:
        """ポートを閉じる."""
        with self._lock:
            self._closed = True
            if self._port.is_open:
                self._port.closePort()

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

    def _write_mode(self, servo_id: int, mode: ServoMode) -> None:
        """MODE を書き、遅れて届く応答を長めに待つ（SDK の待ち時間で切ると、遅れた応答が次の読み出しに混ざる）."""

        def call() -> tuple[list[int], int]:
            result = self._packet.write1ByteTxOnly(
                self._port, servo_id, _ADDR_MODE, int(mode)
            )
            if result != COMM_SUCCESS:
                return [], result
            self._port.setPacketTimeoutMillis(_MODE_REPLY_TIMEOUT_MS)
            return self._packet.rxPacket(self._port)

        packet, result = self._transact(servo_id, call)
        self._check(servo_id, result, packet[_PKT_ERROR] if packet else 0)

    def _write_goal(self, servo_id: int, goal: int) -> None:
        data = [
            _ACC,
            goal & 0xFF,
            (goal >> 8) & 0xFF,
            0,
            0,
            _SPEED & 0xFF,
            _SPEED >> 8,
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
    """

    def __init__(
        self,
        bus: ServoBus,
        *,
        rotation_id: int,
        open_close_id: int,
        closed_position: int,
        open_position: int,
    ) -> None:
        """両方のサーボの応答とモードを確かめ、トルクを入れる.

        Open Close は :func:`hold_position` で今の位置のままトルクを入れる。

        Args:
            bus: サーボのバス
            rotation_id: Rotation（ステップモード）のサーボ ID
            open_close_id: Open Close（位置モード）のサーボ ID
            closed_position: Open Close の閉じた位置（raw 0〜4095）
            open_position: Open Close の開いた位置（raw 0〜4095）

        Raises:
            ServoError: どちらかが応答しないか、モードが役割と合わない場合
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
        bus.set_torque(rotation_id, True)
        hold_position(bus, open_close_id)
        self._bus = bus
        self._rotation_id = rotation_id
        self._open_close_id = open_close_id
        self._closed = closed_position
        self._open = open_position
        self._direction = 1 if open_position > closed_position else -1

    def rotate_by(self, degrees: float) -> None:
        """向きを degrees だけ回す（0 step に丸まれば何も送らない）."""
        steps = degrees_to_steps(degrees)
        if steps != 0:
            self._bus.step(self._rotation_id, steps)

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
        self._bus.goto(self._open_close_id, target)

    def opening(self) -> float:
        """閉じた位置から開く向きへの角度 [deg]."""
        position = self._bus.position(self._open_close_id)
        return (position - self._closed) * self._direction * 360 / STEPS_PER_REV
