"""MCU のデータ辞書: identify の chunk → zlib → JSON、メッセージの符号化と復号、列挙、定数（純粋）.

仕様として読んだもの（コードは写していない。pcbasm は MIT、出典は GPLv3）:

- ``src/firmware/pnp-motion/klipper/docs/Protocol.md`` の「Micro-controller Interface」と「Data Dictionary」
- ファームの C ソース ``klipper/src/command.c``（引数の型ごとの詰め方。文字列は長さ 1 バイト＋中身）、
  ``klipper/src/basecmd.c``（``identify`` と ``identify_response`` の書式）、
  ``klipper/src/rp2040/gpio.c``（ピンの範囲の列挙）

辞書の JSON は ``commands`` と ``responses``（書式 → ID）、``enumerations``（列挙の名前 → 列挙子 → 値。
値が ``[先頭の値, 個数]`` なら、末尾の数字を 1 ずつ増やした名前の範囲）、``config``（定数）を持つ。
ID はビルドが一意に振るので、コマンドと応答で重ならない。メッセージは ID の VLQ に、書式の順に
引数を続けたもの。整数は VLQ、文字列（``%s``・``%*s``・``%.*s``）は長さ 1 バイトと中身。

辞書の ``output``（ファームの ``output()`` のデバッグ文の書式）は読まない。pnp-motion ファームの
運用では使わないので、その ID のブロックは読めないブロックとして捨てる（transport が debug のログに残す）。
"""

from __future__ import annotations

import json
import re
import zlib
from collections.abc import Mapping
from typing import Any

import attrs

from pcbasm.hal.mcu.errors import McuError
from pcbasm.hal.mcu.wire import decode_vlq, encode_vlq

type ParamValue = int | bytes

_IDENTIFY_ID = 1  # Protocol.md でコマンド identify に固定された ID
_IDENTIFY_RESPONSE_ID = 0  # 同じく応答 identify_response の ID

# 整数の型ごとの (bit 数, 符号付きか)。復号した値をこの幅と符号に合わせる
_INT_TYPES: dict[str, tuple[int, bool]] = {
    "%c": (8, False),
    "%hu": (16, False),
    "%hi": (16, True),
    "%u": (32, False),
    "%i": (32, True),
}
# 符号化で受け付ける範囲（下限, 上限 + 1）。線の上ではどの型も 32 bit の VLQ で、ファームは uint32 で
# 受けてから引数の変数に入れる。%c は符号付きの変数（例: stepper.c の ``int_fast8_t invert_step``）にも
# 入るので、-128〜255 を受け付ける
_INT_RANGES: dict[str, tuple[int, int]] = {
    "%c": (-(2**7), 2**8),
    "%hu": (0, 2**16),
    "%hi": (-(2**15), 2**15),
    "%u": (0, 2**32),
    "%i": (-(2**31), 2**31),
}
_BUFFER_TYPES = frozenset({"%s", "%*s", "%.*s"})
_PARAM = re.compile(r"(\w+)=(%\S+)")
_NUMBERED = re.compile(r"(.*?)(\d+)")


@attrs.frozen
class Message:
    """1 つのコマンドまたは応答。``params`` は引数の名前 → 値（整数か bytes）."""

    name: str
    params: Mapping[str, ParamValue]


@attrs.frozen
class _Format:
    msgid: int
    name: str
    params: tuple[tuple[str, str], ...]  # (引数の名前, 型)


def identify_request(offset: int, count: int) -> bytes:
    """辞書の ``offset`` から ``count`` バイトを求める ``identify`` の内容."""
    return encode_vlq(_IDENTIFY_ID) + encode_vlq(offset) + encode_vlq(count)


def parse_identify_response(content: bytes) -> tuple[int, bytes]:
    """``identify_response`` の内容から offset とデータを取り出す."""
    msgid, pos = decode_vlq(content, 0)
    if msgid != _IDENTIFY_RESPONSE_ID:
        raise McuError(f"identify_response ではない（ID {msgid}）")
    offset, pos = decode_vlq(content, pos)
    data, _ = _decode_buffer(content, pos)
    return offset & 0xFFFFFFFF, data


class DataDictionary:
    """MCU のデータ辞書。コマンドの符号化、応答の復号、列挙と定数の参照を受け持つ."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        commands = [
            _parse_format(fmt, msgid) for fmt, msgid in data.get("commands", {}).items()
        ]
        responses = [
            _parse_format(fmt, msgid)
            for fmt, msgid in data.get("responses", {}).items()
        ]
        self._commands = {fmt.name: fmt for fmt in commands}
        self._by_id = {fmt.msgid: fmt for fmt in [*commands, *responses]}
        self._enums = {
            name: _expand_enum(name, values)
            for name, values in data.get("enumerations", {}).items()
        }
        self._constants: dict[str, int | str] = dict(data.get("config", {}))

    @classmethod
    def from_blob(cls, blob: bytes) -> DataDictionary:
        """Identify で集めた blob（zlib 圧縮の JSON）から辞書を作る."""
        try:
            data = json.loads(zlib.decompress(blob))
        except (zlib.error, ValueError) as error:
            raise McuError(f"データ辞書を読めない: {error}") from error
        if not isinstance(data, dict):
            raise McuError("データ辞書が JSON の object ではない")
        return cls(data)

    def constant(self, name: str) -> int | str:
        """定数（ファームの ``DECL_CONSTANT``）。無ければ :class:`McuError`."""
        if name not in self._constants:
            raise McuError(f"データ辞書に定数 {name} が無い")
        return self._constants[name]

    def has_command(self, name: str) -> bool:
        """ファームがコマンド ``name`` を持つか."""
        return name in self._commands

    def encode(self, name: str, **params: ParamValue | str) -> bytes:
        """コマンド ``name`` を詰める.

        引数は書式の名前どおりにすべて渡す。整数の引数に ``str`` を渡すと列挙子の名前として値に直す
        （引数の名前が列挙の名前と同じか、``_<列挙の名前>`` で終わる列挙を使う。``step_pin`` なら ``pin``）。
        ファームに無いコマンドは :class:`McuError`、引数の誤りは ``ValueError``。
        """
        fmt = self._commands.get(name)
        if fmt is None:
            raise McuError(f"ファームにコマンド {name} が無い")
        names = [param for param, _ in fmt.params]
        if sorted(params) != sorted(names):
            raise ValueError(f"{name} の引数は {names}: {sorted(params)}")
        content = bytearray(encode_vlq(fmt.msgid))
        for param, kind in fmt.params:
            content += self._encode_param(name, param, kind, params[param])
        return bytes(content)

    def decode(self, content: bytes) -> list[Message]:
        """ブロックの内容をメッセージの列に分ける。読めなければ :class:`McuError`."""
        messages: list[Message] = []
        pos = 0
        while pos < len(content):
            msgid, pos = decode_vlq(content, pos)
            fmt = self._by_id.get(msgid)
            if fmt is None:
                raise McuError(f"データ辞書に無い ID {msgid}")
            params: dict[str, ParamValue] = {}
            for param, kind in fmt.params:
                params[param], pos = _decode_param(content, pos, kind)
            messages.append(Message(name=fmt.name, params=params))
        return messages

    def enum_value(self, enum: str, name: str) -> int:
        """列挙子 ``name`` の値。列挙が無ければ :class:`McuError`、列挙子が無ければ
        ``ValueError``."""
        values = self._enum(enum)
        if name not in values:
            raise ValueError(f"列挙 {enum} に {name} が無い")
        return values[name]

    def enum_name(self, enum: str, value: int) -> str:
        """値 ``value`` の列挙子の名前（同じ値が複数あれば辞書で先に出るもの）.

        列挙か値が無ければ :class:`McuError`（MCU から届いた値を読むのに使うため）。
        """
        for name, candidate in self._enum(enum).items():
            if candidate == value:
                return name
        raise McuError(f"列挙 {enum} に値 {value} が無い")

    def _enum(self, enum: str) -> dict[str, int]:
        if enum not in self._enums:
            raise McuError(f"データ辞書に列挙 {enum} が無い")
        return self._enums[enum]

    def _encode_param(
        self, command: str, param: str, kind: str, value: ParamValue | str
    ) -> bytes:
        if kind in _BUFFER_TYPES:
            if not isinstance(value, bytes):
                raise ValueError(f"{command} の {param} は bytes: {value!r}")
            return bytes([len(value)]) + value
        if isinstance(value, bytes):
            raise ValueError(f"{command} の {param} は整数: {value!r}")
        if isinstance(value, str):
            value = self._enum_for_param(command, param, value)
        low, high = _INT_RANGES[kind]
        if not low <= value < high:
            raise ValueError(f"{command} の {param}（{kind}）の範囲外: {value}")
        return encode_vlq(value)

    def _enum_for_param(self, command: str, param: str, name: str) -> int:
        matches = [
            enum for enum in self._enums if param == enum or param.endswith(f"_{enum}")
        ]
        if not matches:
            raise ValueError(f"{command} の {param} に列挙は無い: {name!r}")
        return self.enum_value(max(matches, key=len), name)


def _parse_format(fmt: str, msgid: int) -> _Format:
    name, *rest = fmt.split(" ", 1)
    params = tuple(_PARAM.findall(rest[0])) if rest else ()
    for param, kind in params:
        if kind not in _INT_TYPES and kind not in _BUFFER_TYPES:
            raise McuError(f"{name} の {param} の型 {kind} を扱えない")
    return _Format(msgid=msgid, name=name, params=params)


def _expand_enum(enum: str, values: Mapping[str, int | list[int]]) -> dict[str, int]:
    expanded: dict[str, int] = {}
    for name, value in values.items():
        if isinstance(value, int):
            expanded[name] = value
            continue
        numbered = _NUMBERED.fullmatch(name)
        if numbered is None:
            raise McuError(f"列挙 {enum} の範囲 {name} が数字で終わらない")
        prefix, start = numbered.group(1), int(numbered.group(2))
        first, count = value
        for index in range(count):
            expanded[f"{prefix}{start + index}"] = first + index
    return expanded


def _decode_param(content: bytes, pos: int, kind: str) -> tuple[ParamValue, int]:
    if kind in _BUFFER_TYPES:
        return _decode_buffer(content, pos)
    value, pos = decode_vlq(content, pos)
    bits, signed = _INT_TYPES[kind]
    # 読んだ値を、型の幅に切って符号の有無を合わせる
    value %= 1 << bits
    if signed and value >= 1 << (bits - 1):
        value -= 1 << bits
    return value, pos


def _decode_buffer(content: bytes, pos: int) -> tuple[bytes, int]:
    if pos >= len(content):
        raise McuError("文字列の長さが途中で切れている")
    end = pos + 1 + content[pos]
    if end > len(content):
        raise McuError("文字列が途中で切れている")
    return content[pos + 1 : end], end
