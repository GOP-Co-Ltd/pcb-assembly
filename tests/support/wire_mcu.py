"""疑似端末の master 側で MCU（Klipper のプロトコルで話すファーム）を演じる.

``tests/pcbasm/hal/mcu`` のテストが :class:`WireMcu` で子プロセスとして起動する。pytest と同じプロセスの
スレッドで演じると、テスト対象の受信スレッドと GIL を取り合って応答が遅れ、負荷の高いホストで
不安定になるので、別プロセスにする（``wire_servo.py`` と同じ理由）。標準ライブラリだけを使う。

仕様として読んだもの（コードは写していない）: ``src/firmware/pnp-motion/klipper/docs/Protocol.md`` の
「Low-level message encoding」と「Message flow」、ファームの C ソース ``klipper/src/command.c``（ブロックを
受けたら処理してから ack を返す。順番の合わないブロックには、待っている seq の空のブロック = nak を返す）。

子プロセスの使い方: ``python wire_mcu.py <master の fd>``。設定は stdin の最初の 1 行の JSON で渡す。

- 設定は ``dictionary``（辞書の JSON）、``initial_seq``（最初に待つ seq）、``clock_base``（起動したときの clock）
- 起動したら ``ready`` の 1 行を stdout へ書く
- 受けたブロックは 1 行の JSON（``status`` が ``accepted`` / ``dropped`` / ``out_of_order``、``seq``、
  ``content`` の hex、受け付けたものは ``commands`` に名前と引数）で stdout へ書く
- 応答するコマンド: ``identify``、``get_clock``、``get_uptime``、``get_config``、``endstop_query_state``
  （先に別の oid の ``endstop_state`` を返してから、求められた oid の分を返す）、``emergency_stop``
  （shutdown する）、``clear_shutdown``。shutdown の間は、これら以外に ``is_shutdown`` を返す
- stdin の 1 行の JSON で振る舞いを変える（:meth:`WireMcu.control`）:
    - ``{"op": "drop_blocks", "count": n}``: 次の n 個の正しいブロックを、届かなかったことにする
    - ``{"op": "drop_responses", "command": name, "count": n}``: name への応答を n 回捨てる
    - ``{"op": "shutdown", "reason": id}``: shutdown して ``shutdown`` を送る
    - ``{"op": "mute"}``: 以後、何も送らない（ack も応答も）
    - ``{"op": "emit", "name": name, "params": {...}}``: 応答 name を送る
    - ``{"op": "raw", "hex": ...}``: バイト列をそのまま送る
"""

from __future__ import annotations

import json
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
    }
)


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

    def serve(self) -> None:
        buffer = b""
        control = b""
        while True:
            ready, _, _ = select.select([self._master, sys.stdin.fileno()], [], [])
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
                    if kind == "%i" and value >= 2**31:
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
                name, "config", is_config=0, crc=0, is_shutdown=shutdown, move_count=0
            )
        elif name == "endstop_query_state":
            for oid in (params["oid"] + 1, params["oid"]):
                self._reply(
                    name, "endstop_state", oid=oid, homing=0, next_clock=0, pin_value=1
                )
        elif name == "emergency_stop":
            self._enter_shutdown(_COMMAND_REQUEST)
        elif name == "clear_shutdown":
            self._shutdown = None

    def _enter_shutdown(self, reason: int) -> None:
        self._shutdown = reason
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
        self, dictionary: dict[str, Any], *, initial_seq: int = 0, clock_base: int = 0
    ) -> None:
        self._master, self._slave = os.openpty()
        tty.setraw(self._master)
        tty.setraw(self._slave)
        self.path = os.ttyname(self._slave)
        config = {
            "dictionary": dictionary,
            "initial_seq": initial_seq,
            "clock_base": clock_base,
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
