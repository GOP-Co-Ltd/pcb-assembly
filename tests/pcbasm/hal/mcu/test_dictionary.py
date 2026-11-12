"""`pcbasm.hal.mcu.dictionary` の仕様テスト.

データ辞書（identify で受け取る zlib 圧縮の JSON）の読み込みと、それを使ったメッセージの符号化・復号。
辞書は ``tests/support/mcu_dictionary.py`` の合成のもの（ファームのビルド出力は GPLv3 なので置かない）。
期待するバイト列は Protocol.md とファームの ``command.c`` の規則から手で起こしたもの。
"""

import zlib

import pytest

from pcbasm.hal.mcu.dictionary import (
    DataDictionary,
    Message,
    identify_request,
    parse_identify_response,
)
from pcbasm.hal.mcu.errors import McuError
from pcbasm.hal.mcu.wire import encode_vlq
from tests.support.mcu_dictionary import (
    dictionary_blob,
    dictionary_json,
    identify_chunks,
)

_SET_VELOCITY_CONTENT = bytes.fromhex("10 00 822c f818 00 a708 8191d1ac78")


@pytest.fixture
def dictionary() -> DataDictionary:
    return DataDictionary.from_blob(dictionary_blob())


def _identify_response(offset: int, data: bytes) -> bytes:
    return bytes([0]) + encode_vlq(offset) + bytes([len(data)]) + data


class TestIdentify:
    def test_request_bytes(self):
        # identify（ID 1）offset=%u count=%c
        assert identify_request(0, 40) == bytes([0x01, 0x00, 0x28])
        assert identify_request(200, 40) == bytes([0x01, 0x81, 0x48, 0x28])

    def test_parses_a_response(self):
        assert parse_identify_response(_identify_response(200, b"abc")) == (200, b"abc")

    def test_parses_an_empty_response(self):
        assert parse_identify_response(_identify_response(80, b"")) == (80, b"")

    def test_rejects_another_message(self):
        with pytest.raises(McuError):
            parse_identify_response(bytes([0x7F]))

    def test_assembles_the_dictionary_from_chunks(self):
        blob = dictionary_blob()
        data = b"".join(
            parse_identify_response(_identify_response(offset, chunk))[1]
            for offset, chunk in identify_chunks(blob)
        )
        assert DataDictionary.from_blob(data).has_command("velocity_stream_start")


class TestFromBlob:
    def test_rejects_data_that_is_not_zlib(self):
        with pytest.raises(McuError):
            DataDictionary.from_blob(b"not a dictionary")

    def test_rejects_data_that_is_not_json(self):
        with pytest.raises(McuError):
            DataDictionary.from_blob(zlib.compress(b"{"))

    def test_rejects_an_unknown_parameter_type(self):
        data = dictionary_json()
        data["commands"]["odd oid=%q"] = 90
        with pytest.raises(McuError):
            DataDictionary.from_blob(dictionary_blob(data))


class TestConstant:
    def test_int_and_str(self, dictionary: DataDictionary):
        assert dictionary.constant("CLOCK_FREQ") == 12000000
        assert dictionary.constant("MCU") == "rp2040"

    def test_missing_constant_is_an_mcu_error(self, dictionary: DataDictionary):
        with pytest.raises(McuError):
            dictionary.constant("NO_SUCH_CONSTANT")


class TestHasCommand:
    def test_commands(self, dictionary: DataDictionary):
        assert dictionary.has_command("velocity_stream_set_velocity")
        assert dictionary.has_command("get_clock")

    def test_responses_and_unknown_names_are_not_commands(
        self, dictionary: DataDictionary
    ):
        assert not dictionary.has_command("clock")
        assert not dictionary.has_command("queue_step")

    def test_dictionary_without_velocity_stream(self):
        data = dictionary_json()
        data["commands"] = {
            fmt: msgid
            for fmt, msgid in data["commands"].items()
            if not fmt.startswith("velocity_stream")
        }
        assert not DataDictionary.from_blob(dictionary_blob(data)).has_command(
            "velocity_stream_start"
        )


class TestEncode:
    def test_set_velocity_golden(self, dictionary: DataDictionary):
        content = dictionary.encode(
            "velocity_stream_set_velocity",
            oid=0,
            seq=300,
            vx=-1000,
            vy=0,
            vz=5000,
            expire_clock=0x12345678,
        )
        assert content == _SET_VELOCITY_CONTENT

    def test_command_without_parameters(self, dictionary: DataDictionary):
        assert dictionary.encode("get_clock") == bytes([0x05])

    def test_negative_msgid(self, dictionary: DataDictionary):
        assert dictionary.encode("reset") == bytes([0x7C])

    def test_buffer_parameter(self, dictionary: DataDictionary):
        content = dictionary.encode(
            "tmcuart_send", oid=2, write=bytes.fromhex("ea034820e4"), read=0
        )
        assert content == bytes.fromhex("3a 02 05 ea034820e4 00")

    @pytest.mark.parametrize(
        ("pin", "value"),
        [("gpio0", 0), ("gpio11", 11), ("gpio29", 29), ("ADC_TEMPERATURE", 254)],
    )
    def test_pin_names_from_a_range(
        self, dictionary: DataDictionary, pin: str, value: int
    ):
        # step_pin・dir_pin のように「_pin」で終わる引数も列挙 pin を使う
        content = dictionary.encode(
            "config_stepper",
            oid=1,
            step_pin=pin,
            dir_pin="gpio10",
            invert_step=0,
            step_pulse_ticks=0,
        )
        assert content == bytes([31, 1]) + encode_vlq(value) + bytes([10, 0, 0])

    def test_enum_by_exact_name(self, dictionary: DataDictionary):
        assert dictionary.encode(
            "config_endstop", oid=3, pin="gpio5", pull_up=1
        ) == bytes([34, 3, 5, 1])

    def test_enum_whose_name_differs_from_the_parameter(
        self, dictionary: DataDictionary
    ):
        # 引数 kinematics は列挙 velocity_stream_kinematics と名前が合わないので、値に直して渡す
        params = {
            "oid": 4,
            "stepper_x_oid": 1,
            "stepper_y_oid": 2,
            "stepper_z_oid": 3,
            "invert_dir": 5,
            "enable_x_pin": 12,
            "enable_y_pin": 7,
            "enable_z_pin": 2,
            "invert_enable": 7,
        }
        with pytest.raises(ValueError):
            dictionary.encode("config_velocity_stream", **params, kinematics="corexy")
        kinematics = dictionary.enum_value("velocity_stream_kinematics", "corexy")
        content = dictionary.encode(
            "config_velocity_stream", **params, kinematics=kinematics
        )
        assert content == bytes([20, 4, 1, 2, 3, 5, 1, 12, 7, 2, 7])

    def test_negative_byte_value(self, dictionary: DataDictionary):
        # %c は線の上では 32 bit の VLQ。ファームは invert_step を int_fast8_t で受け、-1 で両エッジの step にする
        content = dictionary.encode(
            "config_stepper",
            oid=1,
            step_pin="gpio11",
            dir_pin="gpio10",
            invert_step=-1,
            step_pulse_ticks=0,
        )
        assert content == bytes([31, 1, 11, 10, 0x7F, 0])

    def test_unsigned_32_bit_value(self, dictionary: DataDictionary):
        assert dictionary.encode("finalize_config", crc=0xFFFFFFFF) == bytes([6, 0x7F])

    @pytest.mark.parametrize("pin", ["gpio30", "PA0", "gpio"])
    def test_rejects_an_unknown_pin(self, dictionary: DataDictionary, pin: str):
        with pytest.raises(ValueError):
            dictionary.encode("config_endstop", oid=0, pin=pin, pull_up=0)

    def test_rejects_a_name_for_a_parameter_without_an_enum(
        self, dictionary: DataDictionary
    ):
        with pytest.raises(ValueError):
            dictionary.encode("config_endstop", oid="zero", pin="gpio1", pull_up=0)

    def test_rejects_a_missing_parameter(self, dictionary: DataDictionary):
        with pytest.raises(ValueError):
            dictionary.encode("config_endstop", oid=0, pin="gpio1")

    def test_rejects_an_unknown_parameter(self, dictionary: DataDictionary):
        with pytest.raises(ValueError):
            dictionary.encode("config_endstop", oid=0, pin="gpio1", pull_up=0, extra=1)

    @pytest.mark.parametrize(
        ("name", "params"),
        [
            ("config_endstop", {"oid": 256, "pin": 0, "pull_up": 0}),
            ("config_endstop", {"oid": -129, "pin": 0, "pull_up": 0}),
            (
                "velocity_stream_set_velocity",
                {"oid": 0, "seq": 65536, "vx": 0, "vy": 0, "vz": 0, "expire_clock": 0},
            ),
            (
                "velocity_stream_set_velocity",
                {"oid": 0, "seq": 0, "vx": 2**31, "vy": 0, "vz": 0, "expire_clock": 0},
            ),
            ("finalize_config", {"crc": -1}),
            ("finalize_config", {"crc": 2**32}),
        ],
    )
    def test_rejects_values_outside_the_type(
        self, dictionary: DataDictionary, name: str, params: dict[str, int]
    ):
        with pytest.raises(ValueError):
            dictionary.encode(name, **params)

    def test_rejects_an_int_for_a_buffer(self, dictionary: DataDictionary):
        with pytest.raises(ValueError):
            dictionary.encode("tmcuart_send", oid=0, write=5, read=0)

    def test_unknown_command_is_an_mcu_error(self, dictionary: DataDictionary):
        with pytest.raises(McuError):
            dictionary.encode("queue_step", oid=0, interval=1, count=1, add=0)


class TestDecode:
    def test_response_with_msgid_over_127(self, dictionary: DataDictionary):
        # clock（ID 130）は 2 バイトの ID になる
        content = bytes([0x81, 0x02]) + encode_vlq(0xFFFFFFF0)
        assert dictionary.decode(content) == [
            Message(name="clock", params={"clock": 0xFFFFFFF0})
        ]

    def test_negative_msgid(self, dictionary: DataDictionary):
        content = bytes([0x7F]) + encode_vlq(123) + encode_vlq(70)
        assert dictionary.decode(content) == [
            Message(name="shutdown", params={"clock": 123, "static_string_id": 70})
        ]

    def test_signed_and_unsigned_parameters(self, dictionary: DataDictionary):
        content = (
            bytes([0x81, 0x06, 4])
            + encode_vlq(0x80000000)
            + encode_vlq(65535)
            + bytes([1])
            + b"".join(encode_vlq(v) for v in (-100, 0, 2**31 - 1, -(2**31), 5, -5))
            + bytes([1, 3])
        )
        assert dictionary.decode(content) == [
            Message(
                name="velocity_stream_state",
                params={
                    "oid": 4,
                    "clock": 0x80000000,
                    "seq": 65535,
                    "state": 1,
                    "x": -100,
                    "y": 0,
                    "z": 2**31 - 1,
                    "vx": -(2**31),
                    "vy": 5,
                    "vz": -5,
                    "enabled": 1,
                    "relaxes": 3,
                },
            )
        ]

    def test_buffer_parameter(self, dictionary: DataDictionary):
        content = bytes([0x81, 0x0E, 2, 3]) + b"\x05\xff\x7e"
        assert dictionary.decode(content) == [
            Message(name="tmcuart_response", params={"oid": 2, "read": b"\x05\xff\x7e"})
        ]

    def test_several_messages(self, dictionary: DataDictionary):
        content = bytes([0x7D]) + bytes([0x7E]) + encode_vlq(7)
        assert dictionary.decode(content) == [
            Message(name="starting", params={}),
            Message(name="is_shutdown", params={"static_string_id": 7}),
        ]

    def test_empty_content(self, dictionary: DataDictionary):
        assert dictionary.decode(b"") == []

    def test_round_trips_encoded_commands(self, dictionary: DataDictionary):
        assert dictionary.decode(_SET_VELOCITY_CONTENT) == [
            Message(
                name="velocity_stream_set_velocity",
                params={
                    "oid": 0,
                    "seq": 300,
                    "vx": -1000,
                    "vy": 0,
                    "vz": 5000,
                    "expire_clock": 0x12345678,
                },
            )
        ]

    def test_unknown_msgid_is_an_mcu_error(self, dictionary: DataDictionary):
        with pytest.raises(McuError):
            dictionary.decode(bytes([0x50]))

    def test_truncated_content_is_an_mcu_error(self, dictionary: DataDictionary):
        with pytest.raises(McuError):
            dictionary.decode(bytes([0x81, 0x0E, 2, 3, 0x05]))

    def test_missing_parameter_is_an_mcu_error(self, dictionary: DataDictionary):
        with pytest.raises(McuError):
            dictionary.decode(bytes([0x81, 0x02]))


class TestEnum:
    def test_value_and_name(self, dictionary: DataDictionary):
        assert dictionary.enum_value("velocity_stream_state", "contact") == 3
        assert dictionary.enum_name("velocity_stream_state", 3) == "contact"

    def test_range(self, dictionary: DataDictionary):
        assert dictionary.enum_value("pin", "gpio25") == 25
        assert dictionary.enum_name("pin", 25) == "gpio25"

    def test_shutdown_reason(self, dictionary: DataDictionary):
        assert (
            dictionary.enum_name("static_string_id", 70)
            == "Can't set velocity_stream limits while running"
        )

    def test_unknown_name_is_a_value_error(self, dictionary: DataDictionary):
        with pytest.raises(ValueError):
            dictionary.enum_value("velocity_stream_state", "flying")

    def test_unknown_value_is_an_mcu_error(self, dictionary: DataDictionary):
        with pytest.raises(McuError):
            dictionary.enum_name("velocity_stream_state", 9)

    def test_unknown_enum_is_an_mcu_error(self, dictionary: DataDictionary):
        with pytest.raises(McuError):
            dictionary.enum_value("no_such_enum", "x")
