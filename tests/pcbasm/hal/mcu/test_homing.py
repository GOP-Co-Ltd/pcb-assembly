"""`pcbasm.hal.mcu.homing` の仕様テスト.

MCU 役（``tests/support/wire_mcu.py``）は、物理的な位置でリミットスイッチを押し、張った endstop が trsync を
引くと ``velocity_stream`` を ``contact`` にする。原点出しは 1 回目にスイッチへ向かい、離れてから
2 回目にゆっくり向かい、最後に可動域を戻して位置を ``position_endstop`` にする。
"""

from collections.abc import Iterator
from typing import Any

import attrs
import pytest

from pcbasm.hal.mcu.errors import McuError
from pcbasm.hal.mcu.firmware import PnpMotionFirmware, StreamState, open_firmware
from pcbasm.hal.mcu.homing import home_axis
from pcbasm.hal.mcu.settings import McuMotionSettings
from tests.support.motion_mcu import SWITCHES, motion_settings, open_wire
from tests.support.wire_mcu import WireMcu

_READS = frozenset(
    {
        "identify",
        "get_clock",
        "get_uptime",
        "get_config",
        "velocity_stream_query",
        "endstop_query_state",
        "tmcuart_send",
        "velocity_stream_set_enable",
    }
)

_ARM_X = [
    (
        "trsync_start",
        {"oid": 10, "report_clock": 0, "report_ticks": 0, "expire_reason": 0},
    ),
    ("stepper_stop_on_trigger", {"oid": 1, "trsync_oid": 10}),
    ("stepper_stop_on_trigger", {"oid": 2, "trsync_oid": 10}),
    ("stepper_stop_on_trigger", {"oid": 3, "trsync_oid": 10}),
    ("velocity_stream_stop_on_trigger", {"oid": 0, "trsync_oid": 10}),
    "endstop_home",
    "velocity_stream_start",
    "velocity_stream_set_velocity",
]
_DISARM = ["endstop_home", "endstop_home", "endstop_home"]


def _since(mcu: WireMcu, since: int) -> list[tuple[str, dict[str, Any]]]:
    """読むだけのコマンドを除き、続けて送った速度指令を 1 つにまとめる."""
    commands: list[tuple[str, dict[str, Any]]] = []
    for name, params in mcu.accepted()[since:]:
        if name in _READS:
            continue
        if commands and name == commands[-1][0] == "velocity_stream_set_velocity":
            continue
        commands.append((name, params))
    return commands


def _shape(commands: list[tuple[str, dict[str, Any]]]) -> list[object]:
    """``_ARM_X`` と同じく、引数を確かめないコマンドは名前だけにする."""
    keep = {
        "trsync_start",
        "stepper_stop_on_trigger",
        "velocity_stream_stop_on_trigger",
    }
    return [(name, params) if name in keep else name for name, params in commands]


def _velocities(commands: list[tuple[str, dict[str, Any]]], axis: str) -> list[int]:
    return [
        params[f"v{axis}"]
        for name, params in commands
        if name == "velocity_stream_set_velocity"
    ]


@pytest.fixture
def firmware(settings: McuMotionSettings) -> Iterator[PnpMotionFirmware]:
    opened = open_firmware(settings)
    yield opened
    opened.close()


class TestHomeAxis:
    def test_ends_at_the_endstop_position_with_the_homed_limits(
        self, mcu: WireMcu, firmware: PnpMotionFirmware
    ):
        since = len(mcu.accepted())

        home_axis(firmware, "x")

        commands = _since(mcu, since)
        assert commands[-2:] == [
            (
                "velocity_stream_set_limits",
                {
                    "oid": 0,
                    "axis": 0,
                    "min_pos": 0,
                    "max_pos": 1600,
                    "max_velocity": 8000,
                    "accel": 80000,
                },  # fmt: skip
            ),
            ("velocity_stream_set_position", {"oid": 0, "axis": 0, "pos": 0}),
        ]
        assert firmware.query().position[0] == 0
        assert firmware.shutdown_reason is None

    def test_approaches_backs_off_and_approaches_again(
        self, mcu: WireMcu, firmware: PnpMotionFirmware
    ):
        since = len(mcu.accepted())

        home_axis(firmware, "x")

        shape = _shape(_since(mcu, since))
        assert shape == [
            # 原点出しの前の可動域に広げ、いまの位置を 0 にする
            "velocity_stream_set_limits",
            "velocity_stream_set_position",
            # 1 回目: X のスイッチだけを張って向かい、押された位置を 0 にする
            *_ARM_X,
            *_DISARM,
            "velocity_stream_set_position",
            # 可動域の端を退避の行き先に置いて離れ、止まってから可動域を戻す
            "velocity_stream_set_limits",
            "velocity_stream_start",
            "velocity_stream_set_velocity",
            "velocity_stream_stop",
            "velocity_stream_set_limits",
            # 2 回目
            *_ARM_X,
            *_DISARM,
            "velocity_stream_set_position",
            # 可動域を戻して位置を position_endstop にする
            "velocity_stream_set_limits",
            "velocity_stream_set_position",
        ]

    def test_uses_the_homing_speeds_toward_the_switch(
        self, mcu: WireMcu, firmware: PnpMotionFirmware
    ):
        since = len(mcu.accepted())

        home_axis(firmware, "x")

        # 1 回目は homing_speed（50 mm/s）、離れるのも同じ速さ、2 回目は second_homing_speed（25 mm/s）
        assert _velocities(_since(mcu, since), "x") == [-4000, 4000, -2000]

    def test_arms_only_the_switch_of_the_axis(
        self, mcu: WireMcu, firmware: PnpMotionFirmware
    ):
        since = len(mcu.accepted())

        home_axis(firmware, "z")

        armed = [
            params
            for name, params in _since(mcu, since)
            if name == "endstop_home" and params["sample_count"] > 0
        ]
        assert [params["oid"] for params in armed] == [6, 6]
        assert {params["pin_value"] for params in armed} == {1}
        # Z は正の向きに原点を出す
        assert _velocities(_since(mcu, since), "z") == [4000, -4000, 2000]

    def test_backs_off_first_when_the_switch_is_already_pressed(self):
        wire = open_wire(positions=[-40, 400, -400])
        firmware = open_firmware(motion_settings(wire.path))
        try:
            since = len(wire.accepted())

            home_axis(firmware, "x")

            assert _velocities(_since(wire, since), "x") == [4000, -4000, 4000, -2000]
            assert firmware.query().position[0] == 0
        finally:
            firmware.close()
            wire.close()

    def test_without_a_retract_approaches_once(
        self, mcu: WireMcu, settings: McuMotionSettings
    ):
        settings = attrs.evolve(
            settings, x=attrs.evolve(settings.x, homing_retract=0.0)
        )
        firmware = open_firmware(settings)
        try:
            since = len(mcu.accepted())

            home_axis(firmware, "x")

            assert _velocities(_since(mcu, since), "x") == [-4000]
        finally:
            firmware.close()

    @pytest.mark.slow
    def test_fails_when_the_switch_is_never_pressed(self):
        # X のスイッチがつながっていない
        wire = open_wire(switches=[s for s in SWITCHES if s["axis"] != 0])
        firmware = open_firmware(motion_settings(wire.path))
        try:
            with pytest.raises(McuError) as error:
                home_axis(firmware, "x")

            assert "スイッチ" in str(error.value)
            # 止めて、endstop も外してある（ほかの軸はそのまま使える）
            assert firmware.query().state is StreamState.IDLE
            assert firmware.shutdown_reason is None
            home_axis(firmware, "y")
        finally:
            firmware.close()
            wire.close()
