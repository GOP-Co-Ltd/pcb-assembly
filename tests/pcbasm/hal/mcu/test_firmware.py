"""`pcbasm.hal.mcu.firmware` の仕様テスト.

疑似端末（pty）の向こうで MCU を演じる子プロセス（``tests/support/wire_mcu.py``）と、実物の pyserial で話す。
MCU 役はファームの設定・``velocity_stream``・TMC2209 を模擬し、ファームが shutdown する誤りでは同じく
shutdown するので、送る順番の誤り（止まりきる前の ``set_limits`` など）はテストの失敗として現れる。
"""

import time
from collections.abc import Iterator
from typing import Any

import attrs
import pytest

from pcbasm.hal.mcu.errors import McuError
from pcbasm.hal.mcu.firmware import (
    ContactStop,
    LoadCellFault,
    PnpMotionFirmware,
    StreamState,
    open_firmware,
    send_emergency_stop,
)
from pcbasm.hal.mcu.settings import LoadCellSettings, McuMotionSettings
from tests.support.mcu_dictionary import dictionary_json
from tests.support.motion_mcu import motion_settings, open_wire
from tests.support.polling import wait_until
from tests.support.wire_mcu import WireMcu

# 1 MHz あたりの tick（CLOCK_FREQ は 12 MHz）
_TICKS_PER_SECOND = 12_000_000

# 設定の列（golden）。oid は velocity_stream が 0、stepper が 1〜3、endstop が 4〜6、enable が 7〜9、trsync が 10、
# tmcuart が 11（LED の 12、hx71x の 13、sos_filter の 14、trigger_analog の 15 は、無い機体でも数に入る）。enable_pin は反転（!）なので、励磁を切った値は 1
_CONFIG: list[tuple[str, dict[str, Any]]] = [
    ("allocate_oids", {"count": 16}),
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
    (
        "config_digital_out",
        {"oid": 7, "pin": 12, "value": 1, "default_value": 1, "max_duration": 0},
    ),
    (
        "config_digital_out",
        {"oid": 8, "pin": 7, "value": 1, "default_value": 1, "max_duration": 0},
    ),
    (
        "config_digital_out",
        {"oid": 9, "pin": 2, "value": 1, "default_value": 1, "max_duration": 0},
    ),
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

        assert commands[finalize + 1 :] == _SEARCH_LIMITS

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


# LED（gpio17）を常に点け、HX711（SCK gpio0、データ出力 gpio1、A-128）を読む機体の、trsync の後に足す設定。
# 接触停止: フィルタの断面は 0 個、HX711 の張り付いた ±2^23 の端は範囲の外、50 g × 2666.98 counts/g を gt（1）で
_LED_AND_LOAD_CELL = [
    (
        "config_digital_out",
        {"oid": 12, "pin": 17, "value": 1, "default_value": 1, "max_duration": 0},
    ),
    ("config_hx71x", {"oid": 13, "gain_channel": 1, "dout_pin": 1, "sclk_pin": 0}),
    ("config_sos_filter", {"oid": 14, "max_sections": 0}),
    ("config_trigger_analog", {"oid": 15, "sos_filter_oid": 14}),
    (
        "trigger_analog_set_raw_range",
        {"oid": 15, "raw_min": -8388607, "raw_max": 8388606},
    ),
    (
        "trigger_analog_set_trigger",
        {"oid": 15, "trigger_analog_type": 1, "trigger_value": 133349},
    ),
    ("hx71x_attach_trigger_analog", {"oid": 13, "trigger_analog_oid": 15}),
]


def _with_led_and_load_cell(settings: McuMotionSettings) -> McuMotionSettings:
    return attrs.evolve(
        settings,
        led_pin="gpio17",
        load_cell=LoadCellSettings(
            sclk_pin="gpio0",
            dout_pin="gpio1",
            gain_channel=1,
            sample_rate=80,
            counts_per_gram=2666.98,
        ),
    )


class TestLedAndLoadCell:
    def test_configures_them_after_the_trsync(
        self, mcu: WireMcu, settings: McuMotionSettings
    ):
        firmware = open_firmware(_with_led_and_load_cell(settings))
        try:
            commands = _commands(mcu)
            finalize = [name for name, _ in commands].index("finalize_config")
            trsync = _CONFIG.index(("config_trsync", {"oid": 10})) + 1

            assert commands[:finalize] == [
                *_CONFIG[:trsync],
                *_LED_AND_LOAD_CELL,
                *_CONFIG[trsync:],
            ]
        finally:
            firmware.close()

    def test_without_them_the_config_does_not_mention_them(
        self, mcu: WireMcu, firmware: PnpMotionFirmware
    ):
        names = _names(mcu)

        assert firmware.load_cell is None
        assert "config_hx71x" not in names
        assert "query_hx71x" not in names

    def test_starts_reading_after_connecting_and_stops_on_close(
        self, mcu: WireMcu, settings: McuMotionSettings
    ):
        firmware = open_firmware(_with_led_and_load_cell(settings))
        assert firmware.load_cell is not None
        firmware.close()

        def queries() -> list[dict[str, Any]]:
            return [params for name, params in mcu.accepted() if name == "query_hx71x"]

        wait_until(lambda: len(queries()) == 2, what="query_hx71x を 2 回")
        # 12 MHz を 80 SPS の 16 倍で割った間隔でデータ出力のピンを見させ、閉じるときは 0 で止める
        assert queries() == [
            {"oid": 13, "rest_ticks": 12_000_000 // (80 * 16)},
            {"oid": 13, "rest_ticks": 0},
        ]

    def test_a_failed_open_does_not_stop_a_load_cell_it_never_started(self):
        # 読み始める前に失敗したら、止める命令も送らない（設定の列の途中で失敗したときは、hx71x の oid が
        # MCU に無く、送れば MCU が shutdown する）。ここでは Y のドライバーが応えずに失敗させる
        wire = open_wire(tmc_addresses=[0, 1])
        try:
            with pytest.raises(McuError):
                open_firmware(_with_led_and_load_cell(motion_settings(wire.path)))

            assert "query_hx71x" not in _names(wire)
        finally:
            wire.close()

    def test_starts_reading_again_when_the_config_is_reused(
        self, mcu: WireMcu, settings: McuMotionSettings
    ):
        settings = _with_led_and_load_cell(settings)
        open_firmware(settings).close()
        since = len(mcu.accepted())

        second = open_firmware(settings)
        try:
            assert not second.reconfigured
            # 送るだけで応答を待たないので、MCU 役が受けたのを待つ
            wait_until(lambda: "query_hx71x" in _names(mcu, since), what="query_hx71x")
        finally:
            second.close()


@pytest.fixture
def load_firmware(settings: McuMotionSettings) -> Iterator[PnpMotionFirmware]:
    """ロードセル（2666.98 counts/g、しきい値 50 g）を持つファーム。風袋を取るまで待つ."""
    opened = open_firmware(_with_led_and_load_cell(settings))
    load_cell = opened.load_cell
    assert load_cell is not None
    wait_until(lambda: load_cell.ready, what="風袋引き")
    yield opened
    opened.close()


def _offsets(mcu: WireMcu, since: int = 0) -> list[dict[str, Any]]:
    return [
        params
        for name, params in mcu.accepted()[since:]
        if name == "sos_filter_set_offset_scale"
    ]


class TestContactStop:
    """ロードセルの接触停止: hx71x → trigger_analog（sos_filter）→ 原点出しと同じ trsync."""

    def test_sends_the_tare_as_the_filter_offset(
        self, mcu: WireMcu, load_firmware: PnpMotionFirmware
    ):
        mcu.control("load", counts=1234)
        load_cell = load_firmware.load_cell
        assert load_cell is not None
        wait_until(lambda: load_cell.reading().grams != 0.0, what="荷重の変化")
        time.sleep(0.3)  # 手の風袋引きの 0.25 s の窓を、新しい値だけにする
        since = len(mcu.accepted())

        load_cell.tare()

        wait_until(lambda: bool(_offsets(mcu, since)), what="offset の送信")
        assert _offsets(mcu)[0] == {
            "oid": 14,
            "offset": 0,
            "scale": 1,
            "scale_frac_bits": 0,
            "auto_offset": 0,
        }
        assert _offsets(mcu, since) == [
            {
                "oid": 14,
                "offset": -1234,
                "scale": 1,
                "scale_frac_bits": 0,
                "auto_offset": 0,
            }
        ]

    def test_inverted_sends_a_negative_scale(
        self, mcu: WireMcu, settings: McuMotionSettings
    ):
        with_load_cell = _with_led_and_load_cell(settings)
        assert with_load_cell.load_cell is not None
        firmware = open_firmware(
            attrs.evolve(
                with_load_cell,
                load_cell=attrs.evolve(with_load_cell.load_cell, inverted=True),
            )
        )
        try:
            wait_until(lambda: bool(_offsets(mcu)), what="offset の送信")
            assert _offsets(mcu)[0]["scale"] == -1
        finally:
            firmware.close()

    def test_arms_the_load_cell_on_the_trsync_before_starting(
        self, mcu: WireMcu, load_firmware: PnpMotionFirmware
    ):
        since = len(mcu.accepted())

        load_firmware.start(contact=True)
        load_firmware.stop()
        load_firmware.query()  # 応答を待ち、送ったものが MCU 役の記録に載ってから読む

        commands = _commands(mcu, since)
        names = [name for name, _ in commands]
        assert names[:6] == [
            "trsync_start",
            "stepper_stop_on_trigger",
            "stepper_stop_on_trigger",
            "stepper_stop_on_trigger",
            "velocity_stream_stop_on_trigger",
            "trigger_analog_home",
        ]
        home = commands[5][1]
        start = commands[6][1]
        assert names[6] == "velocity_stream_start"
        # 接触は 4、異常は 5 + RAW_RANGE 0・OVERFLOW 1・MONITOR 2。見張りは 1 サンプルの周期（80 SPS）を 3 回まで
        assert (home["oid"], home["trsync_oid"]) == (15, 10)
        assert (home["trigger_reason"], home["error_reason"]) == (4, 5)
        assert (home["monitor_ticks"], home["monitor_max"]) == (150_000, 3)
        assert 0 < (start["clock"] - home["clock"]) % 2**32 < _TICKS_PER_SECOND
        # 止めたら外す
        assert commands[-1] == (
            "trigger_analog_home",
            {
                "oid": 15,
                "trsync_oid": 10,
                "trigger_reason": 0,
                "error_reason": 0,
                "clock": 0,
                "monitor_ticks": 0,
                "monitor_max": 0,
            },
        )

    def test_refuses_to_arm_before_the_tare(
        self, mcu: WireMcu, settings: McuMotionSettings
    ):
        # 値が来ない（風袋を取れない）ロードセル
        mcu.control("hx71x_stall")
        firmware = open_firmware(_with_led_and_load_cell(settings))
        try:
            firmware.query()
            since = len(mcu.accepted())

            with pytest.raises(LoadCellFault):
                firmware.start(contact=True)

            assert _commands(mcu, since) == []
            assert not firmware.streaming
        finally:
            firmware.close()

    def test_a_press_stops_the_stream_as_contact(
        self, mcu: WireMcu, load_firmware: PnpMotionFirmware
    ):
        load_firmware.start(contact=True)
        load_firmware.set_velocity((0, 0, -800))
        wait_until(lambda: load_firmware.query().position[2] < -40, what="Z が動くこと")

        mcu.control("load", counts=200_000)  # 75 g

        wait_until(
            lambda: load_firmware.settle_trigger() is not None, what="接触の理由"
        )
        assert PnpMotionFirmware.is_contact(load_firmware.trigger_reason)
        assert load_firmware.query().state is StreamState.CONTACT
        assert not load_firmware.streaming

    def test_drive_raises_contact_stop(
        self, mcu: WireMcu, load_firmware: PnpMotionFirmware
    ):
        load_firmware.start(contact=True)
        mcu.control("load", counts=200_000)

        with pytest.raises(ContactStop):
            load_firmware.drive((0, 0, -800), lambda _report: False, 5.0)

    def test_drive_raises_a_fault_when_the_samples_stop(
        self, mcu: WireMcu, load_firmware: PnpMotionFirmware
    ):
        load_firmware.start(contact=True)
        mcu.control("hx71x_stall")

        with pytest.raises(LoadCellFault, match="MONITOR"):
            load_firmware.drive((0, 0, -800), lambda _report: False, 5.0)

        # 異常を知らせたら、理由を忘れて外してある
        assert load_firmware.trigger_reason is None
        assert load_firmware.query().state is StreamState.CONTACT

    def test_a_stuck_sample_is_a_raw_range_fault(
        self, mcu: WireMcu, load_firmware: PnpMotionFirmware
    ):
        load_firmware.start(contact=True)
        mcu.control("load", counts=-(1 << 23))

        with pytest.raises(LoadCellFault, match="RAW_RANGE"):
            load_firmware.drive((0, 0, -800), lambda _report: False, 5.0)

    def test_disarm_also_releases_the_load_cell(
        self, mcu: WireMcu, load_firmware: PnpMotionFirmware
    ):
        since = len(mcu.accepted())

        load_firmware.disarm()
        load_firmware.query()

        assert [name for name, _ in _commands(mcu, since)] == [
            "endstop_home",
            "endstop_home",
            "endstop_home",
            "trigger_analog_home",
        ]

    @pytest.mark.parametrize(
        ("reason", "contact", "error"),
        [
            (None, False, None),
            (1, False, None),
            (3, False, None),
            (4, True, None),
            (5, False, "RAW_RANGE"),
            (6, False, "OVERFLOW"),
            (7, False, "MONITOR"),
            (8, False, "SENSOR_SPECIFIC"),
            (9, False, None),
        ],
    )
    def test_reasons(self, reason: int | None, contact: bool, error: str | None):
        assert PnpMotionFirmware.is_contact(reason) is contact
        assert PnpMotionFirmware.sensor_error(reason) == error


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
        first.start(switch="x")
        first.set_velocity((0, 0, 800))
        wait_until(lambda: first.query().position[2] > 0, what="Z が動き出すこと")
        first.close()
        since = len(mcu.accepted())

        second = open_firmware(settings)
        try:
            commands = _commands(mcu, since)
            assert commands[0] == ("velocity_stream_stop", {"oid": 0})
            assert commands[1:] == [*_DISARM, *_SEARCH_LIMITS]
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
        dictionary["config"]["VELOCITY_STREAM_PROTOCOL"] = 2
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
