"""疑似端末の master 側で MCU（Klipper のプロトコルで話すファーム）を演じる.

``tests/pcbasm/hal/mcu`` のテストが :class:`WireMcu` で子プロセスとして起動する。pytest と同じプロセスの
スレッドで演じると、テスト対象の受信スレッドと GIL を取り合って応答が遅れ、負荷の高いホストで
不安定になるので、別プロセスにする（``wire_servo.py`` と同じ理由）。標準ライブラリだけを使う。

仕様として読んだもの（コードは写していない）: ``src/firmware/pnp-motion/klipper/docs/Protocol.md`` の
「Low-level message encoding」と「Message flow」、ファームの C ソース ``klipper/src/command.c``（ブロックを
受けたら処理してから ack を返す。順番の合わないブロックには、待っている seq の空のブロック = nak を返す）。

子プロセスの使い方: ``python wire_mcu.py <master の fd>``。設定は stdin の最初の 1 行の JSON で渡す。

- 設定は ``dictionary``（辞書の JSON）、``initial_seq``（最初に待つ seq）、``clock_base``（起動したときの clock）、
  ``motion``（下の「ステージの模擬」の設定。省けば既定）
- 起動したら ``ready`` の 1 行を stdout へ書く
- 受けたブロックは 1 行の JSON（``status`` が ``accepted`` / ``dropped`` / ``out_of_order``、``seq``、
  ``content`` の hex、受け付けたものは ``commands`` に名前と引数）で stdout へ書く
- 応答するコマンド: ``identify``、``get_clock``、``get_uptime``、``get_config``、``endstop_query_state``
  （``config_endstop`` していない oid には、先に別の oid の ``endstop_state`` を返してから、求められた oid の分を
  返す）、``emergency_stop``（shutdown する）、``clear_shutdown``、``reset``（設定を消して起動し直したことにする。
  接続は切らない）。shutdown の間は、これら以外に ``is_shutdown`` を返す
- stdin の 1 行の JSON で振る舞いを変える（:meth:`WireMcu.control`）:
    - ``{"op": "drop_blocks", "count": n}``: 次の n 個の正しいブロックを、届かなかったことにする
    - ``{"op": "drop_responses", "command": name, "count": n}``: name への応答を n 回捨てる
    - ``{"op": "shutdown", "reason": id}``: shutdown して ``shutdown`` を送る
    - ``{"op": "mute"}``: 以後、何も送らない（ack も応答も）
    - ``{"op": "emit", "name": name, "params": {...}}``: 応答 name を送る
    - ``{"op": "raw", "hex": ...}``: バイト列をそのまま送る
    - ``{"op": "drivers_off"}``: 以後、TMC2209 はどのアドレスも UART に応えない（モーターの電源を切った）

ステージの模擬（pnp-motion ファームの ``velocity_stream``、``trsync``、``endstop``、``tmcuart``）:

仕様として読んだもの（コードは写していない）: ``src/firmware/pnp-motion/README.md`` の「コマンド」と
「ホストが守る前提」、``module/velocity_stream.c``、``klipper/src`` の ``trsync.c``・``endstop.c``・
``tmcuart.c``・``basecmd.c``（``get_config`` と ``finalize_config``）。

- 設定のコマンドを覚え、``finalize_config`` で ``is_config`` を 1、``crc`` を受けた値にする
- ``velocity_stream`` は、加速度を無視して目標の速度（``max_velocity`` で切る）で位置を積分し、可動域の端で
  止める。期限を過ぎた目標は 0。``velocity_stream_stop`` で ``stopping`` にし、``stop_delay`` 秒後に ``idle`` にする。
  動いている間は ``report_ticks`` ごとに ``velocity_stream_state`` を送る
- ファームが shutdown する誤り（動いている間の ``set_limits``・``set_position``・``start``、可動域の外の位置、
  ``set_limits`` の不正な値（``min_pos >= max_pos``、速度か加速度が 0 か上限を超える）、
  可動域が揃う前の開始、期間の範囲外、登録済みの trsync の signal）は、同じ理由で shutdown する。
  開始の clock が近すぎる誤りは、負荷の高い CI で誤って shutdown しないよう確かめない
- 軸の物理的な位置（stepper の位置）を持ち、``set_position`` は物理的な位置を変えずに座標だけを付け替える
- リミットスイッチ: ``motion.switches`` の各要素 ``{"pin": ピンの番号, "axis": 0〜2, "at": 物理的な位置,
  "above": bool}``。``above`` なら位置が ``at`` 以上、そうでなければ ``at`` 以下で押される（ピンの値は 1）。
  張った ``endstop_home`` の ``pin_value`` と一致したら trsync を引く。登録した ``velocity_stream`` は ``contact``
  になり、引いた軸は ``at`` ちょうどに置く。``trsync_state`` を送る
- 励磁: ``velocity_stream_set_enable`` で入れ・切る（流している間に切ると shutdown）。``velocity_stream_set_idle_timeout``
  の秒数を超えて励磁したまま流していなければ、励磁を切って脱力の回数（``relaxes``）を 1 進め、
  ``velocity_stream_state`` を送る。shutdown でも励磁を切る。模擬の 1 秒の長さは
  ``motion.second``（実時間の秒。既定 1.0）。テストが待つ時間を縮めるのに使う
- 初めの物理的な位置は ``motion.positions``（X・Y・Z のステップ。既定は 0）
- TMC2209: ``motion.tmc_addresses``（既定は 0〜3）のアドレスが UART に応える。書き込みでレジスタを覚えて
  IFCNT を 1 進め、読み出しにはレジスタの値を返す。応えないアドレスへの読み出しには 1 だけのビット列を返す
"""

from __future__ import annotations

import json
import math
import os
import select
import subprocess
import sys
import time
import tty
import zlib
from pathlib import Path
from typing import Any

_SYNC = 0x7E
_SEQ_HIGH = 0x10
_MIN_BLOCK = 5
_MAX_BLOCK = 64
# velocity_stream の上限（README「ホストが守る前提」の「範囲」。CLOCK_FREQ 12 MHz のとき）
_MAX_STEP_RATE = 500_000  # [steps/s]
_MAX_STEP_ACCEL = 1_000_000_000  # [steps/s²]
_BUFFER_TYPES = ("%s", "%*s", "%.*s")
_COMMAND_REQUEST = (
    5  # static_string_id の "Command request"（emergency_stop の shutdown の理由）
)
_ALLOWED_IN_SHUTDOWN = frozenset(
    {
        "identify",
        "get_clock",
        "get_uptime",
        "get_config",
        "emergency_stop",
        "clear_shutdown",
        "reset",
    }
)
_AXES = 3
_MASK32 = 0xFFFFFFFF
_TICK = 0.002  # 動いている間に積分とスイッチを見る間隔 [s]
_TMC_SYNC_REPLY = 0x05
_TMC_MASTER = 0xFF
_IFCNT = 0x02


def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc


def _vlq(value: int) -> bytes:
    if value >= 2**31:
        value -= 2**32
    size = 1
    while not -(32 << 7 * (size - 1)) <= value < (96 << 7 * (size - 1)):
        size += 1
    out = [(value >> 7 * shift) & 0x7F for shift in range(size - 1, -1, -1)]
    return bytes([byte | 0x80 for byte in out[:-1]] + [out[-1]])


def _read_vlq(data: bytes, pos: int) -> tuple[int, int]:
    value = data[pos] & 0x7F
    if data[pos] & 0x60 == 0x60:
        value -= 0x80
    while data[pos] & 0x80:
        pos += 1
        value = (value << 7) | (data[pos] & 0x7F)
    value &= 0xFFFFFFFF
    return value, pos + 1


def _formats(table: dict[str, int]) -> dict[str, tuple[int, list[tuple[str, str]]]]:
    """書式 → ID の表を、名前 → (ID, [(引数, 型)]) にする."""
    parsed = {}
    for fmt, msgid in table.items():
        name, *params = fmt.split()
        parsed[name] = (msgid, [tuple(param.split("=", 1)) for param in params])
    return parsed


def _before(a: int, b: int) -> bool:
    """32 bit の clock で a が b より前か（MCU の timer_is_before と同じ比べ方）."""
    return (a - b) & _MASK32 >= 0x80000000


def _tmc_crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        for i in range(8):
            if (crc >> 7) ^ ((byte >> i) & 1):
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def _uart_frames(data: bytes) -> bytes:
    """Start bit と stop bit を付けて LSB から詰めたビット列から、バイトを取り出す."""
    bits = [(data[i >> 3] >> (i & 7)) & 1 for i in range(len(data) * 8)]
    return bytes(
        sum(bit << i for i, bit in enumerate(bits[k + 1 : k + 9]))
        for k in range(0, len(bits) - 9, 10)
    )


def _uart_bits(data: bytes) -> bytes:
    bits: list[int] = []
    for byte in data:
        bits += [0, *((byte >> i) & 1 for i in range(8)), 1]
    bits += [1] * (-len(bits) % 8)
    return bytes(
        sum(bit << i for i, bit in enumerate(bits[k : k + 8]))
        for k in range(0, len(bits), 8)
    )


class _Stream:
    """``config_velocity_stream`` の 1 つ分の状態（位置と速度は直交座標のステップ）."""

    def __init__(self, oid: int, positions: list[float]) -> None:
        self.oid = oid
        self.state = "idle"
        self.limits: list[tuple[int, int, int, int] | None] = [None] * _AXES
        self.position = [0.0] * _AXES
        self.offset = list(positions)  # 物理的な位置 = position + offset
        self.velocity = [0.0] * _AXES
        self.target = [0] * _AXES
        self.seq = 0
        self.expire = 0
        self.report_ticks = 0
        self.ttl = 0
        self.next_report = 0
        self.stop_at = 0
        self.updated = 0
        self.enabled = False
        self.idle_timeout = 0  # [s]。0 なら脱力しない
        self.idle_since = 0  # 励磁したまま止まり始めた clock
        self.relaxes = 0  # 自分で脱力した回数

    def running(self) -> bool:
        return self.state in ("stream", "stopping")

    def physical(self, axis: int) -> float:
        return self.position[axis] + self.offset[axis]


class _Mcu:
    def __init__(self, master: int, config: dict[str, Any]) -> None:
        dictionary = config["dictionary"]
        self._master = master
        self._blob = zlib.compress(json.dumps(dictionary).encode())
        self._commands = {
            msgid: (name, params)
            for name, (msgid, params) in _formats(dictionary["commands"]).items()
        }
        self._responses = _formats(dictionary["responses"])
        self._frequency = dictionary["config"]["CLOCK_FREQ"]
        self._clock_base = config.get("clock_base", 0)
        self._started = time.monotonic()
        self._next_seq = config.get("initial_seq", 0)
        self._drop_blocks = 0
        self._drop_responses: dict[str, int] = {}
        self._shutdown: int | None = None
        self._muted = False
        self._strings = dictionary["enumerations"].get("static_string_id", {})
        self._states = dictionary["enumerations"].get("velocity_stream_state", {})
        motion = config.get("motion", {})
        self._positions = [float(p) for p in motion.get("positions", [0, 0, 0])]
        self._switches = {
            switch["pin"]: switch for switch in motion.get("switches", [])
        }
        self._tmc_addresses = set(motion.get("tmc_addresses", [0, 1, 2, 3]))
        self._stop_delay = motion.get("stop_delay", 0.03)
        self._second = motion.get("second", 1.0)
        # TMC2209 は MCU と別の部品なので、MCU の reset でレジスタは消えない
        self._tmc = {address: {_IFCNT: 0} for address in self._tmc_addresses}
        self._reset()

    def _reset(self) -> None:
        """設定を消す（起動し直した MCU と同じ状態にする）."""
        self._oids_allocated = False
        self._finalized = False
        self._crc = 0
        self._shutdown = None
        self._endstops: dict[int, int] = {}  # oid → ピン
        self._armed: dict[int, dict[str, int]] = {}  # endstop の oid → 張った値
        self._trsyncs: dict[int, dict[str, Any]] = {}
        self._stream: _Stream | None = None

    def serve(self) -> None:
        buffer = b""
        control = b""
        while True:
            ready, _, _ = select.select(
                [self._master, sys.stdin.fileno()], [], [], self._wait()
            )
            self._tick()
            if sys.stdin.fileno() in ready:
                chunk = os.read(sys.stdin.fileno(), 4096)
                if not chunk:
                    return
                control += chunk
                *lines, control = control.split(b"\n")
                for line in lines:
                    self._control(json.loads(line))
            if self._master in ready:
                try:
                    chunk = os.read(self._master, 4096)
                except OSError:
                    return
                if not chunk:
                    return
                buffer = self._receive(buffer + chunk)

    def _clock(self) -> int:
        elapsed = time.monotonic() - self._started
        return self._clock_base + int(elapsed * self._frequency)

    def _control(self, request: dict[str, Any]) -> None:
        op = request["op"]
        if op == "drop_blocks":
            self._drop_blocks += request["count"]
        elif op == "drop_responses":
            self._drop_responses[request["command"]] = request["count"]
        elif op == "shutdown":
            self._enter_shutdown(request["reason"])
        elif op == "mute":
            self._muted = True
        elif op == "emit":
            self._respond(request["name"], **request["params"])
        elif op == "raw":
            self._write(bytes.fromhex(request["hex"]))
        elif op == "drivers_off":
            self._tmc_addresses = set()

    def _receive(self, buffer: bytes) -> bytes:
        while buffer:
            if buffer[0] == _SYNC:
                buffer = buffer[1:]
                continue
            length = buffer[0]
            if (
                not _MIN_BLOCK <= length <= _MAX_BLOCK
                or len(buffer) >= 2
                and (buffer[1] & 0xF0 != _SEQ_HIGH)
            ):
                buffer = self._resync(buffer)
                continue
            if len(buffer) < length:
                return buffer
            block, rest = buffer[:length], buffer[length:]
            crc = int.from_bytes(block[-3:-1], "big")
            if block[-1] != _SYNC or _crc16(block[:-3]) != crc:
                buffer = self._resync(buffer)
                continue
            buffer = rest
            self._on_block(block[1] & 0x0F, block[2:-3])
        return buffer

    def _resync(self, buffer: bytes) -> bytes:
        index = buffer.find(bytes([_SYNC]))
        self._send_block(b"")
        return b"" if index < 0 else buffer[index + 1 :]

    def _on_block(self, seq: int, content: bytes) -> None:
        record: dict[str, Any] = {"seq": seq, "content": content.hex()}
        if self._drop_blocks:
            self._drop_blocks -= 1
            print(json.dumps({**record, "status": "dropped"}), flush=True)
            return
        if seq != self._next_seq:
            print(json.dumps({**record, "status": "out_of_order"}), flush=True)
            self._send_block(b"")
            return
        self._next_seq = (seq + 1) & 0x0F
        commands = self._decode(content)
        print(
            json.dumps({**record, "status": "accepted", "commands": commands}),
            flush=True,
        )
        for name, params in commands:
            self._dispatch(name, params)
        self._send_block(b"")

    def _decode(self, content: bytes) -> list[tuple[str, dict[str, Any]]]:
        commands = []
        pos = 0
        while pos < len(content):
            msgid, pos = _read_vlq(content, pos)
            msgid = msgid - 2**32 if msgid >= 2**31 else msgid
            name, types = self._commands[msgid]
            params: dict[str, Any] = {}
            for param, kind in types:
                if kind in _BUFFER_TYPES:
                    size = content[pos]
                    params[param] = content[pos + 1 : pos + 1 + size].hex()
                    pos += 1 + size
                else:
                    value, pos = _read_vlq(content, pos)
                    # %c も符号付きの変数に入る（config_stepper の invert_step=-1）
                    if kind in ("%i", "%c") and value >= 2**31:
                        value -= 2**32
                    params[param] = value
            commands.append((name, params))
        return commands

    def _dispatch(self, name: str, params: dict[str, Any]) -> None:
        if self._shutdown is not None and name not in _ALLOWED_IN_SHUTDOWN:
            self._reply(name, "is_shutdown", static_string_id=self._shutdown)
            return
        clock = self._clock()
        if name == "identify":
            offset = params["offset"]
            data = self._blob[offset : offset + params["count"]]
            self._reply(name, "identify_response", offset=offset, data=data)
        elif name == "get_clock":
            self._reply(name, "clock", clock=clock & 0xFFFFFFFF)
        elif name == "get_uptime":
            self._reply(name, "uptime", high=clock >> 32, clock=clock & 0xFFFFFFFF)
        elif name == "get_config":
            shutdown = int(self._shutdown is not None)
            self._reply(
                name,
                "config",
                is_config=int(self._finalized),
                crc=self._crc,
                is_shutdown=shutdown,
                move_count=0,
            )
        elif name == "endstop_query_state":
            self._query_endstop(name, params["oid"])
        elif name == "emergency_stop":
            self._enter_shutdown(_COMMAND_REQUEST)
        elif name == "clear_shutdown":
            self._shutdown = None
        elif name == "reset":
            self._reset()
        else:
            self._dispatch_motion(name, params, clock)

    def _query_endstop(self, name: str, oid: int) -> None:
        if oid in self._endstops:
            self._reply(
                name,
                "endstop_state",
                oid=oid,
                homing=int(oid in self._armed),
                next_clock=0,
                pin_value=self._pin_value(self._endstops[oid]),
            )
            return
        for other in (oid + 1, oid):
            self._reply(
                name, "endstop_state", oid=other, homing=0, next_clock=0, pin_value=1
            )

    def _dispatch_motion(self, name: str, params: dict[str, Any], clock: int) -> None:
        if name == "allocate_oids":
            if self._oids_allocated:
                self._fail("oids already allocated")
            self._oids_allocated = True
        elif name == "finalize_config":
            self._finalized = True
            self._crc = params["crc"]
        elif name == "config_endstop":
            self._endstops[params["oid"]] = params["pin"]
        elif name == "config_trsync":
            self._trsyncs[params["oid"]] = {"can_trigger": False, "signals": []}
        elif name == "config_velocity_stream":
            self._stream = _Stream(params["oid"], self._positions)
        elif name == "tmcuart_send":
            self._tmcuart(name, params)
        elif name == "trsync_start":
            trsync = self._trsyncs[params["oid"]]
            trsync.update(can_trigger=True, signals=[], reason=0)
        elif name in ("stepper_stop_on_trigger", "velocity_stream_stop_on_trigger"):
            signal = (name, params["oid"])
            signals = self._trsyncs[params["trsync_oid"]]["signals"]
            if signal in signals:
                self._fail("Can't add signal that is already active")
                return
            signals.append(signal)
        elif name == "endstop_home":
            if params["sample_count"] == 0:
                self._armed.pop(params["oid"], None)
            else:
                self._armed[params["oid"]] = params
        elif self._stream is not None and name.startswith("velocity_stream_"):
            self._dispatch_stream(self._stream, name, params, clock)

    def _dispatch_stream(
        self, stream: _Stream, name: str, params: dict[str, Any], clock: int
    ) -> None:
        if name == "velocity_stream_set_limits":
            if (
                params["min_pos"] >= params["max_pos"]
                or not 0 < params["max_velocity"] <= _MAX_STEP_RATE
                or not 0 < params["accel"] <= _MAX_STEP_ACCEL
            ):
                self._fail("Invalid velocity_stream limits")
                return
            if stream.running():
                self._fail("Can't set velocity_stream limits while running")
                return
            stream.limits[params["axis"]] = (
                params["min_pos"],
                params["max_pos"],
                params["max_velocity"],
                params["accel"],
            )
        elif name == "velocity_stream_set_position":
            axis, pos = params["axis"], params["pos"]
            limits = stream.limits[axis]
            if limits is None:
                self._fail("velocity_stream limits not set")
            elif stream.running():
                self._fail("Can't set velocity_stream position while running")
            elif not limits[0] <= pos <= limits[1]:
                self._fail("velocity_stream position outside limits")
            else:
                stream.offset[axis] = stream.physical(axis) - pos
                stream.position[axis] = float(pos)
        elif name == "velocity_stream_start":
            self._start(stream, params, clock)
        elif name == "velocity_stream_set_velocity":
            if stream.state != "stream":
                return
            stream.seq = params["seq"]
            stream.target = [params["vx"], params["vy"], params["vz"]]
            latest = (clock + stream.ttl) & _MASK32
            expire = params["expire_clock"]
            stream.expire = latest if _before(latest, expire) else expire
        elif name == "velocity_stream_stop":
            if stream.state == "stream":
                stream.state = "stopping"
                stream.stop_at = clock + int(self._stop_delay * self._frequency)
        elif name == "velocity_stream_query":
            self._report(stream, clock)
        elif name == "velocity_stream_set_enable":
            if not params["enable"] and stream.running():
                self._fail("Can't disable velocity_stream while running")
                return
            stream.enabled = bool(params["enable"])
            stream.idle_since = clock
        elif name == "velocity_stream_set_idle_timeout":
            stream.idle_timeout = params["seconds"]
            stream.idle_since = clock

    def _start(self, stream: _Stream, params: dict[str, Any], clock: int) -> None:
        period, lead, ttl = (
            params["period_ticks"],
            params["lead_ticks"],
            params["ttl_ticks"],
        )
        if stream.running():
            self._fail("velocity_stream already running")
            return
        if any(limits is None for limits in stream.limits):
            self._fail("velocity_stream limits not set")
            return
        if (
            period < self._frequency // 2000
            or not period <= lead <= self._frequency // 10
        ):
            self._fail("Invalid velocity_stream period")
            return
        if not lead + period < ttl <= self._frequency:
            self._fail("Invalid velocity_stream ttl")
            return
        for axis, limits in enumerate(stream.limits):
            assert limits is not None
            pos = math.floor(stream.position[axis])
            if not limits[0] - 1 <= pos <= limits[1] + 1:
                self._fail("velocity_stream position outside limits")
                return
        stream.state = "stream"
        stream.target = [0] * _AXES
        stream.velocity = [0.0] * _AXES
        stream.ttl = ttl
        stream.report_ticks = params["report_ticks"]
        stream.expire = params["clock"]
        stream.next_report = clock
        stream.updated = clock

    def _report(self, stream: _Stream, clock: int) -> None:
        self._respond(
            "velocity_stream_state",
            oid=stream.oid,
            clock=clock & _MASK32,
            seq=stream.seq,
            state=self._states[stream.state],
            x=math.floor(stream.position[0]),
            y=math.floor(stream.position[1]),
            z=math.floor(stream.position[2]),
            vx=round(stream.velocity[0]),
            vy=round(stream.velocity[1]),
            vz=round(stream.velocity[2]),
            enabled=int(stream.enabled),
            relaxes=stream.relaxes,
        )

    def _wait(self) -> float | None:
        stream = self._stream
        if self._armed or (
            stream is not None
            and (stream.running() or stream.enabled and stream.idle_timeout)
        ):
            return _TICK
        return None

    def _tick(self) -> None:
        if self._shutdown is not None:
            return
        clock = self._clock()
        stream = self._stream
        if stream is not None and stream.running():
            self._advance(stream, clock)
        self._check_endstops(clock)
        if stream is not None:
            self._check_idle(stream, clock)
        if stream is not None and stream.running() and stream.report_ticks:
            if clock >= stream.next_report:
                self._report(stream, clock)
                stream.next_report = (
                    max(stream.next_report, clock) + stream.report_ticks
                )

    def _advance(self, stream: _Stream, clock: int) -> None:
        dt = (clock - stream.updated) / self._frequency
        stream.updated = clock
        expired = _before(stream.expire, clock & _MASK32)
        for axis in range(_AXES):
            low, high, max_velocity, _ = stream.limits[axis] or (0, 0, 0, 0)
            target = 0 if stream.state == "stopping" or expired else stream.target[axis]
            velocity = float(max(-max_velocity, min(max_velocity, target)))
            position = stream.position[axis] + velocity * dt
            if position <= low and velocity < 0 or position >= high and velocity > 0:
                position = float(low if velocity < 0 else high)
                velocity = 0.0
            stream.position[axis] = position
            stream.velocity[axis] = velocity
        if stream.state == "stopping" and clock >= stream.stop_at:
            stream.state = "idle"
            stream.velocity = [0.0] * _AXES

    def _check_idle(self, stream: _Stream, clock: int) -> None:
        """励磁したまま流していない時間が ``idle_timeout`` を超えたら、励磁を切って知らせる."""
        if stream.running() or not stream.enabled:
            stream.idle_since = clock
            return
        timeout = stream.idle_timeout * self._second * self._frequency
        if stream.idle_timeout and clock - stream.idle_since > timeout:
            stream.enabled = False
            stream.relaxes = (stream.relaxes + 1) % 256
            self._report(stream, clock)

    def _pin_value(self, pin: int) -> int:
        switch = self._switches.get(pin)
        stream = self._stream
        if switch is None or stream is None:
            return 0
        position = stream.physical(switch["axis"])
        pressed = (
            position >= switch["at"] if switch["above"] else position <= switch["at"]
        )
        return int(pressed)

    def _check_endstops(self, clock: int) -> None:
        for oid, armed in list(self._armed.items()):
            if _before(clock & _MASK32, armed["clock"]):
                continue
            pin = self._endstops[oid]
            if self._pin_value(pin) != armed["pin_value"]:
                continue
            del self._armed[oid]
            self._trigger(armed["trsync_oid"], armed["trigger_reason"], pin, clock)

    def _trigger(self, oid: int, reason: int, pin: int, clock: int) -> None:
        trsync = self._trsyncs[oid]
        if not trsync["can_trigger"]:
            return
        trsync["can_trigger"] = False
        stream = self._stream
        switch = self._switches.get(pin)
        if stream is not None and switch is not None:
            axis = switch["axis"]
            stream.position[axis] = switch["at"] - stream.offset[axis]
        for name, _ in trsync["signals"]:
            if name == "velocity_stream_stop_on_trigger" and stream is not None:
                stream.state = "contact"
                stream.velocity = [0.0] * _AXES
                stream.target = [0] * _AXES
                self._report(stream, clock)
        trsync["signals"] = []
        self._respond(
            "trsync_state",
            oid=oid,
            can_trigger=0,
            trigger_reason=reason,
            clock=clock & _MASK32,
        )

    def _tmcuart(self, name: str, params: dict[str, Any]) -> None:
        datagram = _uart_frames(bytes.fromhex(params["write"]))
        registers = (
            self._tmc.get(datagram[1])
            if len(datagram) >= 4 and datagram[1] in self._tmc_addresses
            else None
        )
        if registers is not None and _tmc_crc8(datagram[:-1]) == datagram[-1]:
            register = datagram[2] & 0x7F
            if len(datagram) == 8 and datagram[2] & 0x80:
                registers[register] = int.from_bytes(datagram[3:7], "big")
                registers[_IFCNT] = (registers[_IFCNT] + 1) & 0xFF
            elif len(datagram) == 4:
                value = registers.get(register, 0)
                reply = bytes([_TMC_SYNC_REPLY, _TMC_MASTER, register])
                reply += value.to_bytes(4, "big")
                reply += bytes([_tmc_crc8(reply)])
                self._reply(
                    name, "tmcuart_response", oid=params["oid"], read=_uart_bits(reply)
                )
                return
        self._reply(
            name, "tmcuart_response", oid=params["oid"], read=b"\xff" * params["read"]
        )

    def _fail(self, reason: str) -> None:
        self._enter_shutdown(self._strings[reason])

    def _enter_shutdown(self, reason: int) -> None:
        self._shutdown = reason
        if self._stream is not None:
            self._stream.state = "idle"
            self._stream.velocity = [0.0] * _AXES
            # ファームは shutdown で脱力し、数えるタイマも消える
            self._stream.enabled = False
            self._stream.idle_timeout = 0
        self._armed.clear()
        for trsync in self._trsyncs.values():
            trsync.update(can_trigger=False, signals=[])
        self._respond(
            "shutdown", clock=self._clock() & 0xFFFFFFFF, static_string_id=reason
        )

    def _reply(self, command: str, response: str, **params: Any) -> None:
        if self._drop_responses.get(command, 0) > 0:
            self._drop_responses[command] -= 1
            return
        self._respond(response, **params)

    def _respond(self, name: str, **params: Any) -> None:
        msgid, types = self._responses[name]
        content = bytearray(_vlq(msgid))
        for param, kind in types:
            value = params[param]
            if kind in _BUFFER_TYPES:
                data = bytes.fromhex(value) if isinstance(value, str) else value
                content += bytes([len(data)]) + data
            else:
                content += _vlq(value)
        self._send_block(bytes(content))

    def _send_block(self, content: bytes) -> None:
        head = bytes([len(content) + _MIN_BLOCK, _SEQ_HIGH | self._next_seq]) + content
        self._write(head + _crc16(head).to_bytes(2, "big") + bytes([_SYNC]))

    def _write(self, data: bytes) -> None:
        if not self._muted:
            os.write(self._master, data)


class WireMcu:
    """テストの側から MCU 役の子プロセスを起動し、受けたブロックを読む.

    slave 側のパス（:attr:`path`）を、テスト対象にポートとして開かせる。
    """

    def __init__(
        self,
        dictionary: dict[str, Any],
        *,
        initial_seq: int = 0,
        clock_base: int = 0,
        motion: dict[str, Any] | None = None,
    ) -> None:
        self._master, self._slave = os.openpty()
        tty.setraw(self._master)
        tty.setraw(self._slave)
        self.path = os.ttyname(self._slave)
        config = {
            "dictionary": dictionary,
            "initial_seq": initial_seq,
            "clock_base": clock_base,
            "motion": motion or {},
        }
        self._process = subprocess.Popen(
            [sys.executable, "-I", str(Path(__file__)), str(self._master)],
            pass_fds=(self._master,),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        assert self._process.stdin is not None and self._process.stdout is not None
        self._process.stdin.write(json.dumps(config).encode() + b"\n")
        self._process.stdin.flush()
        assert self._process.stdout.readline() == b"ready\n"
        self._stdout = self._process.stdout.fileno()
        os.set_blocking(self._stdout, False)
        self._received = b""
        self._records: list[dict[str, Any]] = []
        self._unplugged = False

    def control(self, op: str, **params: Any) -> None:
        """振る舞いを変える（モジュールの docstring の stdin の項）."""
        assert self._process.stdin is not None
        self._process.stdin.write(json.dumps({"op": op, **params}).encode() + b"\n")
        self._process.stdin.flush()

    def records(self) -> list[dict[str, Any]]:
        """受けたブロックの記録（受けた順）."""
        while True:
            try:
                chunk = os.read(self._stdout, 4096)
            except BlockingIOError:
                break
            if not chunk:
                break
            self._received += chunk
        *lines, self._received = self._received.split(b"\n")
        self._records += [json.loads(line) for line in lines]
        return list(self._records)

    def accepted(self) -> list[tuple[str, dict[str, Any]]]:
        """受け付けたコマンド（名前と引数。buffer の引数は hex）を受けた順に."""
        return [
            (name, params)
            for record in self.records()
            if record["status"] == "accepted"
            for name, params in record["commands"]
        ]

    def unplug(self) -> None:
        """線を抜いたことにする（子を止めて master を閉じ、slave 側の I/O を失敗させる）."""
        self._unplugged = True
        self._process.kill()
        self._process.wait()
        os.close(self._master)

    def close(self) -> None:
        if not self._unplugged:
            self.unplug()
        assert self._process.stdin is not None and self._process.stdout is not None
        self._process.stdin.close()
        self._process.stdout.close()
        os.close(self._slave)


if __name__ == "__main__":
    # 設定は大きいので引数ではなく stdin の最初の 1 行で受け取る
    settings = json.loads(sys.stdin.buffer.readline())
    print("ready", flush=True)
    _Mcu(int(sys.argv[1]), settings).serve()
