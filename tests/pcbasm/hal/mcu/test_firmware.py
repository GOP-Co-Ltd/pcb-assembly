"""`pcbasm.hal.mcu.firmware` の仕様テスト.

疑似端末（pty）の向こうで MCU を演じる子プロセス（``tests/support/wire_mcu.py``）と、実物の pyserial で話す。
MCU 役はファームの設定・``velocity_stream``・TMC2209 を模擬し、ファームが shutdown する誤りでは同じく
shutdown するので、送る順番の誤り（止まりきる前の ``set_limits`` など）はテストの失敗として現れる。
"""

import math
from collections.abc import Iterator
from typing import Any

import attrs
import pytest

from pcbasm.hal.mcu.errors import McuError
from pcbasm.hal.mcu.firmware import (
    PnpMotionFirmware,
    StreamState,
    open_firmware,
    send_emergency_stop,
)
from pcbasm.hal.mcu.settings import McuMotionSettings
from tests.support.mcu_dictionary import dictionary_json
from tests.support.motion_mcu import motion_settings, open_wire
from tests.support.polling import wait_until
from tests.support.wire_mcu import WireMcu

# 1 MHz あたりの tick（CLOCK_FREQ は 12 MHz）
_TICKS_PER_SECOND = 12_000_000

# 設定の列（golden）。oid は velocity_stream が 0、stepper が 1〜3、endstop が 4〜6、trsync が 10、tmcuart が 11
# （7〜9 は空き）。enable のピンは velocity_stream が持ち、3 軸とも反転（!）
_CONFIG: list[tuple[str, dict[str, Any]]] = [
    ("allocate_oids", {"count": 12}),
    (
        "config_stepper",
        {
            "oid": 1,
            "step_pin": 11,
            "dir_pin": 10,
            "invert_step": -1,
            "step_pulse_ticks": 0,
        },
    ),
    (
        "config_stepper",
        {
            "oid": 2,
            "step_pin": 6,
            "dir_pin": 5,
            "invert_step": -1,
            "step_pulse_ticks": 0,
        },
    ),
    (
        "config_stepper",
        {
            "oid": 3,
            "step_pin": 19,
            "dir_pin": 28,
            "invert_step": -1,
            "step_pulse_ticks": 0,
        },
    ),
    ("config_endstop", {"oid": 4, "pin": 4, "pull_up": 1}),
    ("config_endstop", {"oid": 5, "pin": 3, "pull_up": 1}),
    ("config_endstop", {"oid": 6, "pin": 25, "pull_up": 1}),
    ("config_trsync", {"oid": 10}),
    (
        "config_tmcuart",
        {"oid": 11, "rx_pin": 9, "pull_up": 0, "tx_pin": 8, "bit_time": 300},
    ),
    (
        "config_velocity_stream",
        {
            "oid": 0,
            "stepper_x_oid": 1,
            "stepper_y_oid": 2,
            "stepper_z_oid": 3,
            "invert_dir": 7,
            "kinematics": 0,
            "enable_x_pin": 12,
            "enable_y_pin": 7,
            "enable_z_pin": 2,
            "invert_enable": 7,
        },
    ),
]

# 原点出しの前の可動域: いまの位置を 0 として、可動域の長さの 2 倍を前後に（X・Y は 20 mm、Z は 10 mm）。
# 速度と加速度は max_velocity 100 mm/s と max_accel 1000 mm/s² を 80 steps/mm で直したもの
_SEARCH_LIMITS = [
    (
        "velocity_stream_set_limits",
        {
            "oid": 0,
            "axis": 0,
            "min_pos": -3200,
            "max_pos": 3200,
            "max_velocity": 8000,
            "accel": 80000,
        },
    ),  # fmt: skip
    (
        "velocity_stream_set_limits",
        {
            "oid": 0,
            "axis": 1,
            "min_pos": -3200,
            "max_pos": 3200,
            "max_velocity": 8000,
            "accel": 80000,
        },
    ),  # fmt: skip
    (
        "velocity_stream_set_limits",
        {
            "oid": 0,
            "axis": 2,
            "min_pos": -1600,
            "max_pos": 1600,
            "max_velocity": 8000,
            "accel": 80000,
        },
    ),  # fmt: skip
    ("velocity_stream_set_position", {"oid": 0, "axis": 0, "pos": 0}),
    ("velocity_stream_set_position", {"oid": 0, "axis": 1, "pos": 0}),
    ("velocity_stream_set_position", {"oid": 0, "axis": 2, "pos": 0}),
]

# 励磁したまま止まっていたら脱力するまでの秒数（既定の 5 分）
_IDLE_TIMEOUT = ("velocity_stream_set_idle_timeout", {"oid": 0, "seconds": 300})

# 張ったままかもしれない endstop を外す
_DISARM = [
    (
        "endstop_home",
        {
            "oid": oid,
            "clock": 0,
            "sample_ticks": 0,
            "sample_count": 0,
            "rest_ticks": 0,
            "pin_value": 0,
            "trsync_oid": 10,
            "trigger_reason": 0,
        },
    )
    for oid in (4, 5, 6)
]

# 状態を読むだけのコマンドと、時刻の同期・識別・TMC の UART（列の golden では見ない）
_READS = frozenset(
    {
        "identify",
        "get_clock",
        "get_uptime",
        "get_config",
        "velocity_stream_query",
        "endstop_query_state",
        "tmcuart_send",
    }
)


def _commands(mcu: WireMcu, since: int = 0) -> list[tuple[str, dict[str, Any]]]:
    return [
        (name, params) for name, params in mcu.accepted()[since:] if name not in _READS
    ]


def _names(mcu: WireMcu, since: int = 0) -> list[str]:
    return [name for name, _ in mcu.accepted()[since:]]


@pytest.fixture
def firmware(settings: McuMotionSettings) -> Iterator[PnpMotionFirmware]:
    opened = open_firmware(settings)
    yield opened
    opened.close()


class TestConfigure:
    def test_sends_the_config_commands_in_order(
        self, mcu: WireMcu, firmware: PnpMotionFirmware
    ):
        commands = _commands(mcu)
        finalize = [name for name, _ in commands].index("finalize_config")

        assert commands[:finalize] == _CONFIG
        assert firmware.reconfigured

    def test_finalizes_with_a_crc_of_the_config(
        self, mcu: WireMcu, firmware: PnpMotionFirmware
    ):
        crcs = [
            params["crc"]
            for name, params in mcu.accepted()
            if name == "finalize_config"
        ]

        assert len(crcs) == 1
        assert crcs[0] != 0

    def test_then_widens_the_limits_around_zero(
        self, mcu: WireMcu, firmware: PnpMotionFirmware
    ):
        commands = _commands(mcu)
        finalize = [name for name, _ in commands].index("finalize_config")

        assert commands[finalize + 1 :] == [*_SEARCH_LIMITS, _IDLE_TIMEOUT]

    def test_writes_the_drivers_before_moving(
        self, mcu: WireMcu, firmware: PnpMotionFirmware
    ):
        names = _names(mcu)

        # 3 軸ぶん、IFCNT を読み、10 のレジスタをそれぞれ書いては IFCNT を読み戻す
        assert names.count("tmcuart_send") == 3 * (1 + 10 * 2)
        assert names.index("tmcuart_send") > names.index("finalize_config")

    def test_reports_idle(self, firmware: PnpMotionFirmware):
        report = firmware.query()

        assert report.state is StreamState.IDLE
        assert report.position == (0, 0, 0)


class TestReuse:
    def test_reuses_the_config_when_the_crc_matches(
        self, mcu: WireMcu, settings: McuMotionSettings
    ):
        open_firmware(settings).close()
        since = len(mcu.accepted())

        second = open_firmware(settings)
        try:
            names = _names(mcu, since)
            assert "reset" not in names
            assert "allocate_oids" not in names
            assert not second.reconfigured
        finally:
            second.close()

    def test_cleans_up_what_the_last_process_left_running(
        self, mcu: WireMcu, settings: McuMotionSettings
    ):
        # 前のプロセスが、endstop を張ったまま流している途中で落ちた
        first = open_firmware(settings)
        first.arm_homing("x")
        first.start()
        first.set_velocity((0, 0, 800))
        wait_until(lambda: first.query().position[2] > 0, what="Z が動き出すこと")
        first.close()
        since = len(mcu.accepted())

        second = open_firmware(settings)
        try:
            commands = _commands(mcu, since)
            assert commands[0] == ("velocity_stream_stop", {"oid": 0})
            assert commands[1:] == [*_DISARM, *_SEARCH_LIMITS, _IDLE_TIMEOUT]
            assert second.query().state is StreamState.IDLE
            assert second.shutdown_reason is None
        finally:
            second.close()

    def test_resets_when_the_config_differs(
        self, mcu: WireMcu, settings: McuMotionSettings
    ):
        open_firmware(settings).close()
        changed = attrs.evolve(
            settings, z=attrs.evolve(settings.z, endstop_pull_up=False)
        )
        since = len(mcu.accepted())

        second = open_firmware(changed)
        try:
            names = _names(mcu, since)
            assert "reset" in names
            assert names.index("allocate_oids") > names.index("reset")
            assert second.reconfigured
        finally:
            second.close()

    def test_resets_after_a_shutdown(self, mcu: WireMcu, settings: McuMotionSettings):
        first = open_firmware(settings)
        first.emergency_stop()
        wait_until(lambda: first.shutdown_reason is not None, what="shutdown")
        first.close()
        since = len(mcu.accepted())

        second = open_firmware(settings)
        try:
            assert "reset" in _names(mcu, since)
            assert second.shutdown_reason is None
            assert second.query().state is StreamState.IDLE
        finally:
            second.close()

    def test_reset_on_request(self, mcu: WireMcu, settings: McuMotionSettings):
        open_firmware(settings).close()
        since = len(mcu.accepted())

        second = open_firmware(settings, reset=True)
        try:
            assert "reset" in _names(mcu, since)
            assert second.reconfigured
        finally:
            second.close()


class TestFirmwareCheck:
    def test_rejects_a_firmware_without_velocity_stream(self):
        dictionary = dictionary_json()
        dictionary["commands"] = {
            fmt: msgid
            for fmt, msgid in dictionary["commands"].items()
            if not fmt.startswith("velocity_stream_")
        }
        wire = WireMcu(dictionary)
        try:
            with pytest.raises(McuError) as error:
                open_firmware(motion_settings(wire.path))
        finally:
            wire.close()

        assert "./install.sh firmware pnp-motion" in str(error.value)

    def test_rejects_another_protocol(self):
        dictionary = dictionary_json()
        dictionary["config"]["VELOCITY_STREAM_PROTOCOL"] = 1
        wire = WireMcu(dictionary)
        try:
            with pytest.raises(McuError) as error:
                open_firmware(motion_settings(wire.path))
        finally:
            wire.close()

        assert "./install.sh firmware pnp-motion" in str(error.value)

    def test_rejects_a_missing_port(self, tmp_path):
        with pytest.raises(McuError):
            open_firmware(motion_settings(str(tmp_path / "no-such-tty")))


class TestDrivers:
    def test_fails_when_a_driver_does_not_answer(self):
        # Y のドライバー（アドレス 2）に電源が無い
        wire = open_wire(tmc_addresses=[0, 1])
        try:
            with pytest.raises(McuError) as error:
                open_firmware(motion_settings(wire.path))
        finally:
            wire.close()

        assert "TMC2209" in str(error.value)
        assert "2" in str(error.value)


class TestEnable:
    def test_energizes_and_relaxes_with_the_firmware_command(
        self, mcu: WireMcu, firmware: PnpMotionFirmware
    ):
        assert not firmware.query().enabled
        since = len(mcu.accepted())

        firmware.set_enabled(True)
        assert firmware.query().enabled
        firmware.set_enabled(False)
        assert not firmware.query().enabled

        assert _commands(mcu, since) == [
            ("velocity_stream_set_enable", {"oid": 0, "enable": 1}),
            ("velocity_stream_set_enable", {"oid": 0, "enable": 0}),
        ]

    def test_sends_the_idle_timeout_in_whole_seconds(
        self, mcu: WireMcu, settings: McuMotionSettings
    ):
        open_firmware(attrs.evolve(settings, idle_timeout=90.4)).close()

        sent = [
            params["seconds"]
            for name, params in mcu.accepted()
            if name == "velocity_stream_set_idle_timeout"
        ]
        assert sent == [90]


class TestSettings:
    @pytest.mark.parametrize("seconds", [0, 0.5, 2.0**32, math.inf])
    def test_idle_timeout_outside_the_firmware_range_is_rejected(
        self, settings: McuMotionSettings, seconds: float
    ):
        with pytest.raises(ValueError, match="idle_timeout"):
            attrs.evolve(settings, idle_timeout=seconds)


class TestIdleTimeout:
    """MCU 役の 1 秒を 0.1 秒に縮め、脱力までの時間を 2 秒（実時間 0.2 秒）にする."""

    @pytest.fixture
    def quick(self) -> Iterator[PnpMotionFirmware]:
        wire = open_wire(second=0.1)
        opened = open_firmware(attrs.evolve(motion_settings(wire.path), idle_timeout=2))
        yield opened
        opened.close()
        wire.close()

    def test_relaxes_by_itself_after_holding_still(self, quick: PnpMotionFirmware):
        quick.set_enabled(True)
        report = quick.query()
        assert report.enabled
        assert report.relaxes == 0

        # 脱力したときにファームが送る報告で知る（問い合わせない）
        wait_until(
            lambda: quick.latest is not None and quick.latest.relaxes == 1,
            what="ファームが自分で脱力すること",
        )
        assert quick.latest is not None
        assert not quick.latest.enabled
        assert quick.query().state is StreamState.IDLE

    def test_holds_while_streaming(self, quick: PnpMotionFirmware):
        quick.set_enabled(True)
        quick.start()
        started = quick.query().clock

        # 期限が切れても流したまま（速度 0）なので、速度を送り直さなくてよい
        wait_until(
            lambda: quick.query().clock > started + 0.6 * _TICKS_PER_SECOND,
            what="脱力までの時間の 3 倍が過ぎること",
        )

        report = quick.query()
        assert report.enabled
        assert report.relaxes == 0
        quick.stop()


class TestStream:
    def test_sends_the_velocity_with_an_expiry_within_the_ttl(
        self, mcu: WireMcu, firmware: PnpMotionFirmware
    ):
        firmware.start()
        firmware.set_velocity((800, -80, 0))
        firmware.stop()

        start = next(
            params for name, params in mcu.accepted() if name == "velocity_stream_start"
        )
        velocity = next(
            params
            for name, params in mcu.accepted()
            if name == "velocity_stream_set_velocity"
        )
        assert (velocity["vx"], velocity["vy"], velocity["vz"]) == (800, -80, 0)
        # 期限は送った時刻 + ttl（0.2 s）。開始は送った時刻 + lead + 50 ms より先
        ahead = (velocity["expire_clock"] - start["clock"]) % 2**32
        assert 0 < ahead < 0.2 * _TICKS_PER_SECOND

    def test_starts_with_the_stream_settings(
        self, mcu: WireMcu, firmware: PnpMotionFirmware
    ):
        firmware.start()
        firmware.stop()

        start = next(
            params for name, params in mcu.accepted() if name == "velocity_stream_start"
        )
        assert start["period_ticks"] == 24_000
        assert start["lead_ticks"] == 120_000
        assert start["report_ticks"] == 600_000
        assert start["ttl_ticks"] == 2_400_000

    def test_moves_and_stops_at_idle(self, firmware: PnpMotionFirmware):
        firmware.start()
        firmware.set_velocity((800, 0, 0))
        wait_until(lambda: firmware.query().position[0] > 0, what="X が動き出すこと")
        firmware.stop()

        report = firmware.query()
        assert report.state is StreamState.IDLE
        assert report.velocity == (0, 0, 0)

    def test_limits_can_be_set_right_after_stop(self, firmware: PnpMotionFirmware):
        firmware.start()
        firmware.set_velocity((800, 0, 0))
        firmware.stop()
        firmware.set_limits("x", -2400, 2400)

        assert firmware.shutdown_reason is None


class TestLimits:
    @pytest.mark.parametrize("bounds", [(5, 5), (6, 5)], ids=["empty", "reversed"])
    def test_rejects_bounds_that_are_not_increasing(
        self, mcu: WireMcu, firmware: PnpMotionFirmware, bounds: tuple[int, int]
    ):
        since = len(mcu.accepted())

        with pytest.raises(ValueError):
            firmware.set_limits("x", *bounds)

        # 送ると MCU が shutdown する値なので、送らない
        assert _commands(mcu, since) == []
        assert firmware.shutdown_reason is None


class TestTravel:
    def _at(self, firmware: PnpMotionFirmware, steps: int, low: int, high: int) -> None:
        """X を ``steps`` に置き、可動域を ``low``〜``high`` にする（位置は可動域の外でもよい）."""
        firmware.set_limits("x", min(low, steps), max(high, steps) + 1)
        firmware.set_position("x", steps)
        firmware.set_limits("x", low, high)
        firmware.query()  # 応答を待ち、送ったものが MCU 役の記録に載ってから数え始める

    def test_moves_to_the_target_and_restores_the_limits(
        self, mcu: WireMcu, firmware: PnpMotionFirmware
    ):
        self._at(firmware, 0, 0, 1600)
        since = len(mcu.accepted())

        firmware.travel({"x": 800})

        assert abs(firmware.query().position[0] - 800) <= 1
        limits = [
            (params["min_pos"], params["max_pos"])
            for name, params in _commands(mcu, since)
            if name == "velocity_stream_set_limits"
        ]
        # 向かう側の端（上限）を行き先に置き、止まってから元の可動域に戻す
        assert limits == [(0, 800), (0, 1600)]
        assert firmware.query().state is StreamState.IDLE

    def test_runs_at_the_given_speed(self, mcu: WireMcu, firmware: PnpMotionFirmware):
        self._at(firmware, 0, 0, 1600)
        since = len(mcu.accepted())

        firmware.travel({"x": 80}, speed=50.0)

        sent = {
            params["vx"]
            for name, params in _commands(mcu, since)
            if name == "velocity_stream_set_velocity"
        }
        assert sent == {4000}  # 50 mm/s を 80 steps/mm で直したもの

    @pytest.mark.parametrize("target", [0, 1], ids=["one-step-above", "two-steps"])
    def test_does_not_move_an_axis_reported_a_step_below_its_lower_end(
        self, mcu: WireMcu, firmware: PnpMotionFirmware, target: int
    ):
        # corexy では、下端で止まった軸が 1 ステップ下（-1）と報告されうる。ここから下端（0）へ向かうと、
        # 向かう側の端を行き先に置く可動域が 0〜0 になって MCU が shutdown する
        self._at(firmware, -1, 0, 1600)
        since = len(mcu.accepted())

        firmware.travel({"x": target})

        assert firmware.shutdown_reason is None
        if target == 0:
            assert _commands(mcu, since) == []
        else:
            assert abs(firmware.query().position[0] - target) <= 1

    def test_reports_arrive_while_streaming(self, firmware: PnpMotionFirmware):
        firmware.start()
        firmware.set_velocity((800, 0, 0))
        wait_until(
            lambda: (latest := firmware.latest) is not None
            and latest.state is StreamState.STREAM,
            what="動いている間の定期の報告",
        )
        firmware.stop()


class TestEmergencyStop:
    def test_shuts_the_mcu_down(self, firmware: PnpMotionFirmware):
        firmware.emergency_stop()

        wait_until(lambda: firmware.shutdown_reason is not None, what="shutdown")
        assert firmware.shutdown_reason == "Command request"

    def test_opening_for_it_sends_nothing_else(self, mcu: WireMcu):
        send_emergency_stop(mcu.path)

        assert set(_names(mcu)) == {"identify", "emergency_stop"}

    def test_opening_for_it_needs_a_port(self, tmp_path):
        with pytest.raises(McuError):
            send_emergency_stop(str(tmp_path / "no-such-tty"))
