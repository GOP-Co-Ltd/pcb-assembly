"""`pcbasm.hal.mcu.wire` の仕様テスト.

MCU との線上の形（CRC16、VLQ、メッセージブロック）。期待するバイト列は Protocol.md と、ファームの
``command.c``・``crc16_ccitt.c`` の規則から手で起こしたもの。
"""

import pytest

from pcbasm.hal.mcu.errors import McuError
from pcbasm.hal.mcu.wire import (
    MAX_BLOCK,
    MAX_CONTENT,
    SYNC,
    Block,
    BlockReader,
    crc16_ccitt,
    decode_vlq,
    encode_block,
    encode_vlq,
)

# velocity_stream_set_velocity（ID 16）oid=0 seq=300 vx=-1000 vy=0 vz=5000 expire_clock=0x12345678
_SET_VELOCITY_CONTENT = bytes.fromhex("10 00 822c f818 00 a708 8191d1ac78")
_SET_VELOCITY_BLOCK = bytes.fromhex("13 13 10 00 822c f818 00 a708 8191d1ac78 5359 7e")
_ACK_SEQ0 = bytes.fromhex("05 10 9e81 7e")


class TestConstants:
    def test_block_limits(self):
        assert (MAX_BLOCK, MAX_CONTENT, SYNC) == (64, 59, 0x7E)


class TestCrc16Ccitt:
    def test_check_value(self):
        # CRC-16/MCRF4XX（初期値 0xFFFF、反転の多項式 0x8408、最後の XOR なし）の検査値
        assert crc16_ccitt(b"123456789") == 0x6F91

    def test_empty(self):
        assert crc16_ccitt(b"") == 0xFFFF


class TestVlq:
    @pytest.mark.parametrize(
        ("value", "size"),
        [
            (0, 1),
            (-32, 1),
            (95, 1),
            (96, 2),
            (-33, 2),
            (12287, 2),
            (-4096, 2),
            (12288, 3),
            (-4097, 3),
            (1572863, 3),
            (-524288, 3),
            (1572864, 4),
            (-524289, 4),
            (201326591, 4),
            (-67108864, 4),
            (201326592, 5),
            (-67108865, 5),
            (2**31 - 1, 5),
            (-(2**31), 5),
        ],
    )
    def test_round_trips_with_the_documented_size(self, value: int, size: int):
        encoded = encode_vlq(value)
        assert len(encoded) == size
        assert decode_vlq(encoded, 0) == (value, size)

    def test_unsigned_above_int32_decodes_as_signed(self):
        # MCU は 32 bit の値を符号付きに見て詰める。0xFFFFFFFF は -1 と同じ 1 バイトになる
        assert encode_vlq(0xFFFFFFFF) == bytes([0x7F])
        assert decode_vlq(encode_vlq(0xFFFFFFFF), 0) == (-1, 1)
        assert decode_vlq(encode_vlq(0x80000000), 0) == (-(2**31), 5)

    @pytest.mark.parametrize(
        ("value", "encoded"),
        [
            (300, "822c"),
            (-1000, "f818"),
            (5000, "a708"),
            (0x12345678, "8191d1ac78"),
            (-4, "7c"),
        ],
    )
    def test_known_bytes(self, value: int, encoded: str):
        assert encode_vlq(value) == bytes.fromhex(encoded)

    def test_decodes_from_the_given_position(self):
        data = b"\xff" + encode_vlq(300) + encode_vlq(-1)
        assert decode_vlq(data, 1) == (300, 3)
        assert decode_vlq(data, 3) == (-1, 4)

    @pytest.mark.parametrize("value", [2**32, -(2**31) - 1])
    def test_rejects_values_outside_32_bits(self, value: int):
        with pytest.raises(ValueError):
            encode_vlq(value)

    def test_truncated_data_is_an_mcu_error(self):
        with pytest.raises(McuError):
            decode_vlq(bytes([0x82]), 0)

    def test_too_long_data_is_an_mcu_error(self):
        with pytest.raises(McuError):
            decode_vlq(bytes([0x81] * 5 + [0x01]), 0)


class TestEncodeBlock:
    def test_known_block(self):
        assert encode_block(3, _SET_VELOCITY_CONTENT) == _SET_VELOCITY_BLOCK

    def test_empty_block_is_an_ack(self):
        assert encode_block(0, b"") == _ACK_SEQ0

    def test_uses_the_low_4_bits_of_seq(self):
        assert encode_block(16 + 3, _SET_VELOCITY_CONTENT) == _SET_VELOCITY_BLOCK

    def test_largest_content(self):
        block = encode_block(0, bytes(MAX_CONTENT))
        assert len(block) == MAX_BLOCK

    def test_rejects_too_large_content(self):
        with pytest.raises(ValueError):
            encode_block(0, bytes(MAX_CONTENT + 1))


class TestBlockReader:
    def test_reads_a_block(self):
        assert BlockReader().feed(_SET_VELOCITY_BLOCK) == [
            Block(seq=3, content=_SET_VELOCITY_CONTENT)
        ]

    def test_reads_an_empty_block(self):
        assert BlockReader().feed(_ACK_SEQ0) == [Block(seq=0, content=b"")]

    def test_reads_a_block_split_into_single_bytes(self):
        reader = BlockReader()
        blocks = [
            block
            for byte in _SET_VELOCITY_BLOCK
            for block in reader.feed(bytes([byte]))
        ]
        assert blocks == [Block(seq=3, content=_SET_VELOCITY_CONTENT)]

    def test_reads_consecutive_blocks_and_skips_leading_sync(self):
        data = bytes([SYNC, SYNC]) + _ACK_SEQ0 + _SET_VELOCITY_BLOCK
        assert BlockReader().feed(data) == [
            Block(seq=0, content=b""),
            Block(seq=3, content=_SET_VELOCITY_CONTENT),
        ]

    def test_round_trips_encode_block(self):
        content = bytes(range(MAX_CONTENT))
        assert BlockReader().feed(encode_block(9, content)) == [
            Block(seq=9, content=content)
        ]

    def test_drops_a_block_with_a_broken_crc(self):
        broken = bytearray(_SET_VELOCITY_BLOCK)
        broken[-2] ^= 0x01
        assert BlockReader().feed(bytes(broken) + _ACK_SEQ0) == [
            Block(seq=0, content=b"")
        ]

    def test_drops_a_block_with_invalid_seq_high_bits(self):
        broken = bytearray(_ACK_SEQ0)
        broken[1] = 0x20
        assert BlockReader().feed(bytes(broken) + _ACK_SEQ0) == [
            Block(seq=0, content=b"")
        ]

    def test_drops_a_block_without_trailing_sync(self):
        broken = bytearray(_ACK_SEQ0)
        broken[-1] = 0x00
        assert BlockReader().feed(bytes(broken) + bytes([SYNC]) + _ACK_SEQ0) == [
            Block(seq=0, content=b"")
        ]

    @pytest.mark.parametrize("length", [0x04, 0x41])
    def test_drops_bytes_with_an_invalid_length(self, length: int):
        garbage = bytes([length, 0x10, 0x01, 0x02, SYNC])
        assert BlockReader().feed(garbage + _ACK_SEQ0) == [Block(seq=0, content=b"")]

    def test_resyncs_at_the_next_sync_across_feeds(self):
        # 壊れたブロックの途中から読み始め、次の 0x7E まで捨てて同期を取り直す
        reader = BlockReader()
        assert reader.feed(_SET_VELOCITY_BLOCK[5:12]) == []
        assert reader.feed(_SET_VELOCITY_BLOCK[12:]) == []
        assert reader.feed(_SET_VELOCITY_BLOCK) == [
            Block(seq=3, content=_SET_VELOCITY_CONTENT)
        ]

    def test_discards_garbage_without_sync_until_one_arrives(self):
        reader = BlockReader()
        assert reader.feed(bytes([0x01, 0x02, 0x03, 0x04, 0x05, 0x06])) == []
        assert reader.feed(bytes([0x10, 0x20])) == []
        assert reader.feed(bytes([SYNC]) + _ACK_SEQ0) == [Block(seq=0, content=b"")]

    def test_waits_for_the_rest_of_a_block(self):
        reader = BlockReader()
        assert reader.feed(_SET_VELOCITY_BLOCK[:-1]) == []
        assert reader.feed(_SET_VELOCITY_BLOCK[-1:]) == [
            Block(seq=3, content=_SET_VELOCITY_CONTENT)
        ]
