"""`pcbasm.hal.mcu.transport` の仕様テスト.

疑似端末（pty）の向こうで MCU を演じる子プロセス（``tests/support/wire_mcu.py``）と、実物の pyserial で話す。
辞書は ``tests/support/mcu_dictionary.py`` の合成のもの。
"""

import threading
import time

import pytest

from pcbasm.hal.mcu.dictionary import Message
from pcbasm.hal.mcu.errors import McuError, McuShutdown
from pcbasm.hal.mcu.transport import McuTransport
from tests.support.mcu_dictionary import dictionary_blob, dictionary_json
from tests.support.polling import before_deadline, wait_until
from tests.support.wire_mcu import WireMcu

# 負荷の高い CI でも誤って接続断・期限切れにしない長さ
_PATIENT = 10.0


@pytest.fixture
def wire():
    mcu = WireMcu(dictionary_json())
    yield mcu
    mcu.close()


def _open(
    wire: WireMcu,
    *,
    retransmit_timeout: float = 0.1,
    disconnect_timeout: float = _PATIENT,
) -> McuTransport:
    return McuTransport(
        wire.path,
        retransmit_timeout=retransmit_timeout,
        disconnect_timeout=disconnect_timeout,
    )


@pytest.fixture
def transport(wire: WireMcu):
    mcu = _open(wire)
    mcu.identify(timeout=_PATIENT)
    yield mcu
    mcu.close()


def _names(wire: WireMcu) -> list[str]:
    return [name for name, _ in wire.accepted()]


def _send_raises(
    transport: McuTransport, error: type[McuError], name: str = "get_clock"
) -> bool:
    params = {"oid": 0} if name == "endstop_query_state" else {}
    try:
        transport.send(name, **params)
    except error:
        return True
    return False


def _thread_names() -> list[str]:
    return [thread.name for thread in threading.enumerate()]


class TestIdentify:
    def test_downloads_the_dictionary_in_chunks(self, wire: WireMcu):
        transport = _open(wire)
        try:
            dictionary = transport.identify(timeout=_PATIENT)
        finally:
            transport.close()

        assert dictionary.constant("CLOCK_FREQ") == 12000000
        offsets = [
            params["offset"] for name, params in wire.accepted() if name == "identify"
        ]
        size = len(dictionary_blob())
        # 末尾を過ぎた offset で空のデータを受け取るまで、前から順に求める
        assert offsets[0] == 0
        assert offsets[-1] >= size
        assert offsets == sorted(set(offsets))

    def test_dictionary_is_the_identified_one(self, transport: McuTransport):
        assert transport.dictionary.has_command("velocity_stream_set_velocity")

    def test_dictionary_before_identify_is_an_error(self, wire: WireMcu):
        transport = _open(wire)
        try:
            with pytest.raises(McuError, match="identify"):
                _ = transport.dictionary
        finally:
            transport.close()

    @pytest.mark.parametrize("initial_seq", [0, 1, 7, 15])
    def test_follows_the_sequence_the_mcu_expects(self, initial_seq: int):
        # 前のホストが話した後の MCU は、0 以外の seq を待っている
        wire = WireMcu(dictionary_json(), initial_seq=initial_seq)
        transport = _open(wire)
        try:
            transport.identify(timeout=_PATIENT)
            transport.send("get_uptime")

            wait_until(lambda: "get_uptime" in _names(wire), what="get_uptime の受理")
        finally:
            transport.close()
            wire.close()


class TestSend:
    def test_sends_the_encoded_command_in_a_block(
        self, wire: WireMcu, transport: McuTransport
    ):
        transport.send(
            "velocity_stream_set_velocity",
            oid=0,
            seq=300,
            vx=-1000,
            vy=0,
            vz=5000,
            expire_clock=0x12345678,
        )

        wait_until(lambda: "velocity_stream_set_velocity" in _names(wire))
        record = next(
            record
            for record in wire.records()
            if record["status"] == "accepted"
            and record["commands"][0][0] == "velocity_stream_set_velocity"
        )
        assert record["content"] == "1000822cf81800a7088191d1ac78"

    def test_numbers_blocks_in_order(self, wire: WireMcu, transport: McuTransport):
        for oid in range(20):  # 4 bit の seq を 1 周以上させる
            transport.send("endstop_query_state", oid=oid)

        wait_until(lambda: _names(wire).count("endstop_query_state") == 20)
        oids = [
            params["oid"]
            for name, params in wire.accepted()
            if name == "endstop_query_state"
        ]
        assert oids == list(range(20))
        assert all(record["status"] == "accepted" for record in wire.records())

    def test_retransmits_a_lost_block_after_the_timeout(self, wire: WireMcu):
        transport = _open(wire, retransmit_timeout=0.1)
        try:
            transport.identify(timeout=_PATIENT)
            wire.control("drop_blocks", count=1)

            transport.send("get_uptime")

            wait_until(lambda: "get_uptime" in _names(wire), what="送り直し")
        finally:
            transport.close()
        statuses = [record["status"] for record in wire.records()]
        assert statuses.count("dropped") == 1

    def test_retransmits_at_once_on_nak(self, wire: WireMcu):
        # 期限による送り直しは待たない。MCU が順番の合わないブロックに返す nak で送り直す
        transport = _open(wire, retransmit_timeout=60.0, disconnect_timeout=120.0)
        try:
            transport.identify(timeout=_PATIENT)
            wire.control("drop_blocks", count=1)

            transport.send("get_uptime")
            transport.send("get_clock")

            wait_until(
                lambda: _names(wire)[-2:] == ["get_uptime", "get_clock"],
                timeout=_PATIENT,
                what="nak による送り直し",
            )
        finally:
            transport.close()

    def test_emergency_stop_does_not_wait_for_the_window(self, wire: WireMcu):
        # ack を待つブロックが window に達していても、emergency_stop は後ろに並ばずすぐ線に出る
        transport = McuTransport(
            wire.path, retransmit_timeout=60.0, window=1, disconnect_timeout=120.0
        )
        try:
            transport.identify(timeout=_PATIENT)
            wire.control("drop_blocks", count=1)

            transport.send("get_uptime")  # 届かず、ack を待ったまま
            transport.send("get_clock")  # window が埋まっているので並ぶ
            transport.send("emergency_stop")

            # emergency_stop が線に出ると、MCU は順番の合わないそれに nak を返し、送り直しで追いつく
            wait_until(
                lambda: "get_clock" in _names(wire),
                timeout=_PATIENT,
                what="emergency_stop による送り直し",
            )
        finally:
            transport.close()
        names = _names(wire)
        assert names.index("emergency_stop") < names.index("get_clock")

    def test_unknown_command_is_an_error(self, transport: McuTransport):
        with pytest.raises(McuError, match="no_such_command"):
            transport.send("no_such_command")


class TestQuery:
    def test_returns_the_response(self, transport: McuTransport):
        message = transport.query("get_clock", response="clock", timeout=_PATIENT)

        assert message.name == "clock"
        assert isinstance(message.params["clock"], int)

    def test_resends_when_the_response_is_lost(
        self, wire: WireMcu, transport: McuTransport
    ):
        wire.control("drop_responses", command="get_uptime", count=1)

        message = transport.query("get_uptime", response="uptime", timeout=_PATIENT)

        assert message.name == "uptime"
        assert _names(wire).count("get_uptime") >= 2

    def test_sends_the_oid_and_waits_for_the_response_of_that_oid(
        self, wire: WireMcu, transport: McuTransport
    ):
        # MCU 役は、求めた oid の応答の前に別の oid の応答を返す
        message = transport.query(
            "endstop_query_state", response="endstop_state", oid=2, timeout=_PATIENT
        )

        assert message.params["oid"] == 2
        assert ("endstop_query_state", {"oid": 2}) in wire.accepted()

    def test_does_not_resend_while_the_block_waits_for_an_ack(self, wire: WireMcu):
        # 届かなかったブロックは期限で送り直す。問い合わせの側でもう 1 つ送ると、MCU が 2 度実行する
        transport = _open(wire, retransmit_timeout=0.6)
        try:
            transport.identify(timeout=_PATIENT)
            wire.control("drop_blocks", count=1)

            transport.query("get_uptime", response="uptime", timeout=_PATIENT)
        finally:
            transport.close()

        assert _names(wire).count("get_uptime") == 1

    def test_times_out_without_a_response(self, wire: WireMcu, transport: McuTransport):
        wire.control("drop_responses", command="get_clock", count=1000)

        with pytest.raises(McuError, match="clock"):
            transport.query("get_clock", response="clock", timeout=0.5)


class TestSubscribe:
    def test_delivers_responses_of_the_oid_with_the_receive_time(
        self, wire: WireMcu, transport: McuTransport
    ):
        received: list[tuple[Message, float]] = []
        transport.subscribe(
            "endstop_state", lambda message, at: received.append((message, at)), oid=1
        )
        before = time.monotonic()

        for oid in (2, 1):
            wire.control(
                "emit",
                name="endstop_state",
                params={"oid": oid, "homing": 0, "next_clock": 0, "pin_value": oid},
            )

        wait_until(lambda: len(received) == 1)
        message, at = received[0]
        assert message.params == {
            "oid": 1,
            "homing": 0,
            "next_clock": 0,
            "pin_value": 1,
        }
        assert before <= at <= time.monotonic()

    def test_without_oid_receives_every_oid(
        self, wire: WireMcu, transport: McuTransport
    ):
        oids: list[int] = []
        transport.subscribe(
            "endstop_state",
            lambda message, _: oids.append(int(message.params["oid"])),
        )

        for oid in (2, 1):
            wire.control(
                "emit",
                name="endstop_state",
                params={"oid": oid, "homing": 0, "next_clock": 0, "pin_value": 0},
            )

        wait_until(lambda: oids == [2, 1])

    def test_subscribing_again_replaces_the_callback(
        self, wire: WireMcu, transport: McuTransport
    ):
        first: list[Message] = []
        second: list[Message] = []
        transport.subscribe("endstop_state", lambda message, _: first.append(message))
        transport.subscribe("endstop_state", lambda message, _: second.append(message))

        wire.control(
            "emit",
            name="endstop_state",
            params={"oid": 0, "homing": 0, "next_clock": 0, "pin_value": 0},
        )

        wait_until(lambda: len(second) == 1)
        assert first == []

    def test_keeps_receiving_after_a_callback_raises(
        self, wire: WireMcu, transport: McuTransport
    ):
        def fail(message: Message, at: float) -> None:
            raise RuntimeError("callback の失敗")

        transport.subscribe("endstop_state", fail)
        wire.control(
            "emit",
            name="endstop_state",
            params={"oid": 0, "homing": 0, "next_clock": 0, "pin_value": 0},
        )

        message = transport.query("get_clock", response="clock", timeout=_PATIENT)

        assert message.name == "clock"

    def test_drops_unsubscribed_messages_and_broken_blocks(
        self, wire: WireMcu, transport: McuTransport
    ):
        wire.control("emit", name="stats", params={"count": 1, "sum": 2, "sumsq": 3})
        wire.control("raw", hex="0710ffffffff7e")  # CRC の合わないブロック

        message = transport.query("get_clock", response="clock", timeout=_PATIENT)

        assert message.name == "clock"


class TestShutdown:
    def test_send_raises_after_the_mcu_shuts_down(self, transport: McuTransport):
        transport.send("emergency_stop")

        wait_until(
            lambda: _send_raises(transport, McuShutdown, "endstop_query_state"),
            what="McuShutdown",
        )
        with pytest.raises(McuShutdown) as error:
            transport.query(
                "endstop_query_state",
                response="endstop_state",
                oid=0,
                timeout=_PATIENT,
            )
        assert error.value.reason == "Command request"

    def test_commands_accepted_in_shutdown_still_pass(
        self, wire: WireMcu, transport: McuTransport
    ):
        # ファームが shutdown の間も受け付けるコマンド（HF_IN_SHUTDOWN）は送れる
        transport.send("emergency_stop")
        wait_until(
            lambda: _send_raises(transport, McuShutdown, "endstop_query_state"),
            what="McuShutdown",
        )

        transport.send("emergency_stop")
        transport.send("clear_shutdown")
        message = transport.query("get_clock", response="clock", timeout=_PATIENT)

        assert message.name == "clock"
        assert _names(wire).count("emergency_stop") == 2
        assert "clear_shutdown" in _names(wire)

    def test_clear_shutdown_lets_other_commands_pass_again(
        self, transport: McuTransport
    ):
        transport.send("emergency_stop")
        wait_until(
            lambda: _send_raises(transport, McuShutdown, "endstop_query_state"),
            what="McuShutdown",
        )

        transport.send("clear_shutdown")
        wait_until(
            lambda: not _send_raises(transport, McuShutdown, "endstop_query_state"),
            what="clear_shutdown の ack",
        )
        message = transport.query(
            "endstop_query_state", response="endstop_state", oid=0, timeout=_PATIENT
        )

        assert message.params["oid"] == 0

    def test_pending_query_raises(self, wire: WireMcu, transport: McuTransport):
        wire.control("drop_responses", command="endstop_query_state", count=1000)
        failures: list[BaseException] = []

        def query() -> None:
            try:
                transport.query(
                    "endstop_query_state",
                    response="endstop_state",
                    oid=0,
                    timeout=_PATIENT,
                )
            except McuError as error:
                failures.append(error)

        worker = threading.Thread(target=query, daemon=True)
        worker.start()
        wait_until(lambda: "endstop_query_state" in _names(wire))
        wire.control("shutdown", reason=70)
        worker.join(_PATIENT)

        assert len(failures) == 1
        assert isinstance(failures[0], McuShutdown)
        assert "limits" in failures[0].reason

    def test_is_shutdown_reply_raises(self, wire: WireMcu):
        # shutdown 中の MCU に開き直したとき、受け付けないコマンドへの is_shutdown で知る
        wire.control("shutdown", reason=70)
        transport = _open(wire)
        try:
            transport.identify(timeout=_PATIENT)

            with pytest.raises(McuShutdown, match="limits"):
                transport.query(
                    "endstop_query_state",
                    response="endstop_state",
                    oid=0,
                    timeout=_PATIENT,
                )
        finally:
            transport.close()


class TestDisconnect:
    def test_raises_when_acks_stop(self, wire: WireMcu):
        transport = _open(wire, disconnect_timeout=0.5)
        try:
            transport.identify(timeout=_PATIENT)
            wire.control("mute")

            with pytest.raises(McuError, match="ack"):
                before_deadline(
                    lambda: transport.query(
                        "get_clock", response="clock", timeout=60.0
                    ),
                    what="接続断の検出",
                )
            with pytest.raises(McuError, match="ack"):
                transport.send("get_clock")
        finally:
            transport.close()

    def test_raises_when_the_port_is_gone(self, wire: WireMcu):
        transport = _open(wire)
        try:
            transport.identify(timeout=_PATIENT)
            wire.unplug()

            wait_until(lambda: _send_raises(transport, McuError), what="McuError")
        finally:
            transport.close()

    def test_cannot_open_a_missing_port(self, tmp_path):
        with pytest.raises(McuError, match="開けない"):
            McuTransport(str(tmp_path / "ttyACM9"))


class TestClose:
    def test_stops_the_receive_thread(self, wire: WireMcu):
        transport = _open(wire)
        transport.identify(timeout=_PATIENT)
        assert f"mcu-transport {wire.path}" in _thread_names()

        transport.close()

        assert f"mcu-transport {wire.path}" not in _thread_names()

    def test_send_after_close_is_an_error(self, transport: McuTransport):
        transport.close()
        transport.close()  # 2 度閉じてもよい

        with pytest.raises(McuError, match="閉じ"):
            transport.send("get_clock")
