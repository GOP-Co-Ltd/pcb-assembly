"""MCU との線上の形: CRC16、VLQ、メッセージブロックの組み立てと分解（純粋）.

仕様として読んだもの（コードは写していない。pcbasm は MIT、出典は GPLv3）:

- ``src/firmware/pnp-motion/klipper/docs/Protocol.md`` の「Low-level message encoding」
- ファームの C ソース ``klipper/src/command.c``（VLQ の詰め方、ブロックの検査の順）と
  ``klipper/src/generic/crc16_ccitt.c``（CRC の種類）

ブロックは ``<長さ 1 バイト><seq 1 バイト><内容 n バイト><CRC 2 バイト><0x7E>``。長さは見出しと末尾を
含めた全体で 5〜64。seq は下位 4 bit が番号で、上位 4 bit は常に 0x1。CRC は見出しと内容に掛け、
上位バイトを先に置く。内容が空のブロックは ack / nak になる。
"""

from __future__ import annotations

import attrs

from pcbasm.hal.mcu.errors import McuError

# ブロックの最大の長さ（見出しと末尾を含む）
MAX_BLOCK = 64
# ブロックに入る内容の最大のバイト数（64 から見出し 2 と末尾 3 を引いたもの）
MAX_CONTENT = 59
# ブロックの終わりを示すバイト
SYNC = 0x7E

_HEADER = 2
_TRAILER = 3
_MIN_BLOCK = _HEADER + _TRAILER
_SEQ_MASK = 0x0F
_SEQ_HIGH = 0x10
_VLQ_MAX_BYTES = 5  # 32 bit は 7 bit ずつ 5 バイトに収まる


def crc16_ccitt(data: bytes) -> int:
    """ブロックの CRC（CRC-16/MCRF4XX: 初期値 0xFFFF、反転の多項式 0x8408、最後の XOR なし）."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc


def encode_vlq(value: int) -> bytes:
    """32 bit の整数を VLQ に詰める.

    -2^31〜2^32-1 を受け付ける。2^31 以上は 32 bit の 2 の補数で負の数と同じに詰める（MCU と同じ）。
    7 bit ずつ上位から並べ、最後のバイト以外は最上位 bit を立てる。先頭のバイトの bit 6〜5 が両方立って
    いれば負の数とみなす約束なので、正の数は先頭がそうならない長さまで延ばす（-32〜95 が 1 バイト）。
    """
    if not -(2**31) <= value < 2**32:
        raise ValueError(f"VLQ に詰められるのは 32 bit の整数だけ: {value}")
    signed = value - 2**32 if value >= 2**31 else value
    size = 1
    # size バイトで表せるのは -32·128^(size-1) 〜 96·128^(size-1) - 1
    while size < _VLQ_MAX_BYTES and not -(32 << 7 * (size - 1)) <= signed < (
        96 << 7 * (size - 1)
    ):
        size += 1
    groups = [(signed >> 7 * shift) & 0x7F for shift in reversed(range(size))]
    return bytes([group | 0x80 for group in groups[:-1]] + [groups[-1]])


def decode_vlq(data: bytes, pos: int) -> tuple[int, int]:
    """``data[pos:]`` から VLQ を 1 つ読み、値（32 bit の符号付き）と次の位置を返す.

    ``%u`` などの符号なしとして読むかは呼び出し側（辞書）が決める。途中で切れていたり 5 バイトを
    超えたりしていれば :class:`McuError`。
    """
    if pos >= len(data):
        raise McuError("VLQ が途中で切れている")
    first = data[pos]
    value = first & 0x7F
    if first & 0x60 == 0x60:
        value -= 0x80  # 負の数: 先頭の 7 bit を符号付きで読む
    end = pos + 1
    byte = first
    while byte & 0x80:
        if end >= len(data):
            raise McuError("VLQ が途中で切れている")
        if end - pos >= _VLQ_MAX_BYTES:
            raise McuError("VLQ が 5 バイトを超える")
        byte = data[end]
        value = (value << 7) | (byte & 0x7F)
        end += 1
    value &= 0xFFFFFFFF
    return (value - 2**32 if value >= 2**31 else value), end


def encode_block(seq: int, content: bytes) -> bytes:
    """内容をブロックに包む。``seq`` は下位 4 bit を使う."""
    if len(content) > MAX_CONTENT:
        raise ValueError(f"ブロックの内容は {MAX_CONTENT} バイトまで: {len(content)}")
    head = bytes([len(content) + _MIN_BLOCK, _SEQ_HIGH | (seq & _SEQ_MASK)]) + content
    return head + crc16_ccitt(head).to_bytes(2, "big") + bytes([SYNC])


@attrs.frozen
class Block:
    """受け取ったブロック。``seq`` は下位 4 bit の番号、``content`` は空なら ack / nak."""

    seq: int
    content: bytes


class BlockReader:
    """受け取ったバイト列をブロックに分ける.

    長さ・seq の上位 bit・末尾の 0x7E・CRC のどれかが合わないブロックは、次の 0x7E までを捨てて
    同期を取り直す。ブロックの前に置かれた余分な 0x7E は読み飛ばす。
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._need_sync = False

    def feed(self, data: bytes) -> list[Block]:
        """``data`` を足し、そろったブロックを順に返す（足りない分は次の呼び出しまで持つ）."""
        self._buffer += data
        blocks: list[Block] = []
        while self._buffer:
            if self._need_sync:
                self._discard_through_sync()
                continue
            if self._buffer[0] == SYNC:
                del self._buffer[0]
                continue
            length = self._buffer[0]
            if not _MIN_BLOCK <= length <= MAX_BLOCK:
                self._need_sync = True
                continue
            if len(self._buffer) < _HEADER:
                break
            if self._buffer[1] & ~_SEQ_MASK != _SEQ_HIGH:
                self._need_sync = True
                continue
            if len(self._buffer) < length:
                break
            block = bytes(self._buffer[:length])
            if block[-1] != SYNC or crc16_ccitt(block[:-_TRAILER]) != int.from_bytes(
                block[-_TRAILER:-1], "big"
            ):
                self._need_sync = True
                continue
            del self._buffer[:length]
            blocks.append(
                Block(seq=block[1] & _SEQ_MASK, content=block[_HEADER:-_TRAILER])
            )
        return blocks

    def _discard_through_sync(self) -> None:
        index = self._buffer.find(SYNC)
        if index < 0:
            self._buffer.clear()
            return
        del self._buffer[: index + 1]
        self._need_sync = False
