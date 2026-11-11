"""`pcbasm.hal.mcu.tmc2209` の仕様テスト.

TMC2209 に書くレジスタの値、UART の datagram、ファームの tmcuart_send
に渡すビット列を、実バイト列で確かめる。 kurousagi の printer.cfg（run_current 0.4
A、microsteps 64、rotation_distance 32、stealthchop_threshold 999999）と
Config_Reference.md の ``driver_*`` の既定値で、Klipper が書いていたのと同じ
レジスタの値になることを golden にする。
"""

import pytest

from pcbasm.hal.mcu.tmc2209 import (
    CHOPCONF,
    GCONF,
    IFCNT,
    IHOLD_IRUN,
    PWMCONF,
    SLAVECONF,
    TPWMTHRS,
    TmcSettings,
    crc8,
    current_bits,
    decode_read_reply,
    encode_uart,
    read_request,
    register_values,
    write_datagram,
)

_KUROUSAGI_STEP_DISTANCE = 32 / (
    200 * 64
)  # rotation_distance / (full steps × microsteps)

# アドレス 0 の IFCNT を読む要求と、GCONF に 0x1C0 を書く要求を、ファームへ渡す形にしたもの
_READ_IFCNT_UART = "ea034820e4"
_WRITE_GCONF_UART = "ea03083080000a08b887"

# TMC2209 からの応答（sync 0x05、master 0xFF、IFCNT、値 5、CRC 0x25）をファームが受け取った形
_REPLY_IFCNT_5 = "0afa4f20800002a8a092"


class TestRegisterValues:
    def test_kurousagi_matches_klipper(self):
        values = register_values(TmcSettings(run_current=0.4), _KUROUSAGI_STEP_DISTANCE)

        assert values[GCONF] == 0x000001C0
        assert values[CHOPCONF] == 0x32030053
        assert values[IHOLD_IRUN] == 0x00080C0C
        assert values[PWMCONF] == 0xC80D0E24
        assert values[TPWMTHRS] == 0
        assert (
            values[SLAVECONF] == 0x00000200
        )  # senddelay=2（3 つのドライバーが UART を共有する）

    def test_gconf_is_written_first(self):
        values = register_values(TmcSettings(run_current=0.4), _KUROUSAGI_STEP_DISTANCE)

        assert next(iter(values)) == GCONF

    def test_stealthchop_threshold_becomes_tstep(self):
        # TSTEP = 12 MHz × microsteps × step_distance / (256 × v) = 12e6 × 64 × 0.0025 / (256 × 50) = 150
        values = register_values(
            TmcSettings(run_current=0.4, stealthchop_threshold=50.0),
            _KUROUSAGI_STEP_DISTANCE,
        )

        assert values[TPWMTHRS] == 150

    def test_zero_threshold_selects_spreadcycle(self):
        values = register_values(
            TmcSettings(run_current=0.4, stealthchop_threshold=0.0),
            _KUROUSAGI_STEP_DISTANCE,
        )

        assert values[GCONF] == 0x000001C4  # en_SpreadCycle（bit 2）

    @pytest.mark.parametrize(
        ("microsteps", "mres"), [(256, 0), (128, 1), (64, 2), (16, 4), (1, 8)]
    )
    def test_microsteps_sets_mres(self, microsteps, mres):
        values = register_values(
            TmcSettings(run_current=0.4, microsteps=microsteps), 32 / (200 * microsteps)
        )

        assert (values[CHOPCONF] >> 24) & 0x0F == mres

    def test_rejects_microsteps_not_power_of_two(self):
        with pytest.raises(ValueError, match="microsteps"):
            register_values(
                TmcSettings(run_current=0.4, microsteps=48), _KUROUSAGI_STEP_DISTANCE
            )

    def test_hold_current_sets_ihold(self):
        values = register_values(
            TmcSettings(run_current=0.4, hold_current=0.2), _KUROUSAGI_STEP_DISTANCE
        )

        assert values[IHOLD_IRUN] & 0x1F == 6  # ihold（0.2 A は CS ≒ 5.5 を四捨五入）
        assert (values[IHOLD_IRUN] >> 8) & 0x1F == 12  # irun


class TestCurrentBits:
    def test_kurousagi_uses_high_sensitivity(self):
        assert current_bits(0.4, 0.4, 0.110) == (12, 12, True)

    def test_switches_to_low_sensitivity_when_irun_exceeds_31(self):
        # vsense=1 なら CS = 32 × √2 × 1.0 × 0.13 / 0.18 − 1 ≒ 31.7 で 31 を超える。
        # vsense=0 なら CS = 32 × √2 × 1.0 × 0.13 / 0.325 − 1 ≒ 17.1
        assert current_bits(1.0, 0.5, 0.110) == (17, 8, False)

    def test_rejects_current_beyond_full_scale(self):
        with pytest.raises(ValueError, match="full scale"):
            current_bits(2.0, 2.0, 0.110)


class TestCrc8:
    @pytest.mark.parametrize(
        ("data", "crc"),
        [
            ("f50002", 0xC8),
            ("f5008000 0001c0", 0x0F),
            ("05ff0200000005", 0x25),
        ],
    )
    def test_known_values(self, data, crc):
        assert crc8(bytes.fromhex(data)) == crc


class TestDatagram:
    def test_read_request(self):
        assert read_request(0, IFCNT) == bytes.fromhex("f50002c8")

    def test_write_datagram(self):
        assert write_datagram(0, GCONF, 0x1C0) == bytes.fromhex("f5008000 0001c00f")

    def test_write_datagram_value_is_big_endian(self):
        assert write_datagram(2, CHOPCONF, 0x32030053)[3:7] == bytes.fromhex("32030053")


class TestEncodeUart:
    def test_read_request_golden(self):
        assert encode_uart(read_request(0, IFCNT)).hex() == _READ_IFCNT_UART

    def test_write_golden(self):
        assert encode_uart(write_datagram(0, GCONF, 0x1C0)).hex() == _WRITE_GCONF_UART

    def test_starts_with_sync_pattern_the_firmware_detects(self):
        # ファームの tmcuart.c は先頭 6 bit が 0x2a のときに sync nibble から baud を合わせる
        assert encode_uart(read_request(3, IFCNT))[0] & 0x3F == 0x2A


class TestDecodeReadReply:
    def test_decodes_register_and_value(self):
        assert decode_read_reply(bytes.fromhex(_REPLY_IFCNT_5)) == (IFCNT, 5)

    def test_round_trips_encoded_reply(self):
        reply = bytes.fromhex("05ff6c32030053")
        reply += bytes([crc8(reply)])

        assert decode_read_reply(encode_uart(reply)) == (CHOPCONF, 0x32030053)

    def test_rejects_crc_mismatch(self):
        reply = bytes.fromhex("05ff0200000006") + bytes(
            [0x25]
        )  # 値を変えて CRC はそのまま

        assert decode_read_reply(encode_uart(reply)) is None

    def test_rejects_bad_stop_bit(self):
        data = bytearray.fromhex(_REPLY_IFCNT_5)
        data[1] &= ~0x02  # 1 byte 目の stop bit（bit 9）を 0 にする

        assert decode_read_reply(bytes(data)) is None

    def test_rejects_bad_start_bit(self):
        data = bytearray.fromhex(_REPLY_IFCNT_5)
        data[0] |= 0x01  # 1 byte 目の start bit（bit 0）を 1 にする

        assert decode_read_reply(bytes(data)) is None

    def test_rejects_empty_reply(self):
        # 応答がなければ、ファームは read を空で返す
        assert decode_read_reply(b"") is None
