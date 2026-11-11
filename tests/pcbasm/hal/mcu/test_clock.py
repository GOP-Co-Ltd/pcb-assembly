"""`pcbasm.hal.mcu.clock` の仕様テスト.

:class:`ClockEstimator` は合成した標本で、:class:`ClockSync` は疑似端末の向こうの MCU 役
（``tests/support/wire_mcu.py``）と実物の :class:`McuTransport` で確かめる。
"""

import threading

import pytest

from pcbasm.hal.mcu.clock import ClockEstimator, ClockSync, to_clock32
from pcbasm.hal.mcu.errors import McuError
from pcbasm.hal.mcu.transport import McuTransport
from tests.support.mcu_dictionary import dictionary_json
from tests.support.polling import wait_until
from tests.support.wire_mcu import WireMcu

FREQUENCY = 12_000_000
_PATIENT = 10.0


def _truth(t: float, *, frequency: float = FREQUENCY, base: int = 0) -> int:
    """時刻 t に MCU が数えていた clock（t=0 で base）."""
    return base + round(t * frequency)


class TestToClock32:
    @pytest.mark.parametrize(
        ("clock", "clock32"),
        [(0, 0), (2**32 - 1, 2**32 - 1), (2**32, 0), (5 * 2**32 + 7, 7)],
    )
    def test_keeps_the_low_32_bits(self, clock: int, clock32: int):
        assert to_clock32(clock) == clock32


class TestClockEstimator:
    def test_needs_a_sample(self):
        with pytest.raises(McuError, match="標本"):
            ClockEstimator(FREQUENCY).clock_at(0.0)

    def test_first_sample_is_taken_as_64_bit_at_the_midpoint(self):
        estimator = ClockEstimator(FREQUENCY)

        estimator.add_sample(10.0, 10.002, 5 * 2**32 + 100)

        assert estimator.clock_at(10.001) == 5 * 2**32 + 100
        # 標本が 1 つなら公称の周波数で進める
        assert estimator.clock_at(10.501) == 5 * 2**32 + 100 + FREQUENCY // 2

    def test_estimates_the_frequency_from_samples(self):
        # 水晶の誤差で公称より 200 ppm 速い MCU
        frequency = FREQUENCY * (1 + 200e-6)
        estimator = ClockEstimator(FREQUENCY)

        for second in range(10):
            sent = float(second)
            estimator.add_sample(
                sent, sent + 0.002, _truth(sent + 0.001, frequency=frequency)
            )

        # 最後の標本から 5 秒先でも、1 tick の桁で合う（公称のままなら 2400 tick ずれる）
        assert abs(estimator.clock_at(14.0) - _truth(14.0, frequency=frequency)) <= 2

    def test_anchors_on_the_sample_with_the_smallest_round_trip(self):
        estimator = ClockEstimator(FREQUENCY)
        # 応答の帰りだけが遅れた標本（中点が読んだ時刻より 24 ms 後ろへずれる）
        for second in range(5):
            sent = float(second)
            estimator.add_sample(sent, sent + 0.05, _truth(sent + 0.001))
        # 往復の短い標本が 1 つある
        estimator.add_sample(5.0, 5.0004, _truth(5.0002))
        for second in range(6, 10):
            sent = float(second)
            estimator.add_sample(sent, sent + 0.05, _truth(sent + 0.001))

        # 誤差は往復の短い標本の半分（0.2 ms）に収まる。中点を平均すると 24 ms ずれる
        assert abs(estimator.clock_at(10.0) - _truth(10.0)) <= 0.0002 * FREQUENCY

    def test_round_trip_outliers_do_not_tilt_the_frequency(self):
        estimator = ClockEstimator(FREQUENCY)
        for second in range(0, 10, 3):
            sent = float(second)
            estimator.add_sample(sent, sent + 0.001, _truth(sent + 0.0005))
        # 1 つだけ大きく遅れた標本（行きが遅れ、中点が読んだ時刻より 0.2 s 前へずれる）
        estimator.add_sample(10.0, 10.5, _truth(10.45))

        assert abs(estimator.clock_at(20.0) - _truth(20.0)) <= 0.0002 * FREQUENCY

    def test_extends_32_bit_samples_across_the_wrap(self):
        base = 2**32 - FREQUENCY // 2  # 0.5 秒後に 32 bit が折り返す
        estimator = ClockEstimator(FREQUENCY)
        estimator.add_sample(0.0, 0.002, _truth(0.001, base=base))

        for second in range(1, 4):
            sent = float(second)
            estimator.add_sample(
                sent, sent + 0.002, to_clock32(_truth(sent + 0.001, base=base))
            )

        assert estimator.clock_at(3.001) == _truth(3.001, base=base)
        assert estimator.clock_at(3.001) > 2**32

    @pytest.mark.parametrize("offset", [-1000, 0, 1000, 2**30])
    def test_extend_picks_the_nearest_value(self, offset: int):
        last = 2**32 + 500  # 折り返した直後の標本
        estimator = ClockEstimator(FREQUENCY)
        estimator.add_sample(0.0, 0.0, last)

        assert estimator.extend(to_clock32(last + offset)) == last + offset


@pytest.fixture
def wire():
    # 0.2 秒後に 32 bit の clock が折り返す MCU
    mcu = WireMcu(dictionary_json(), clock_base=2**32 - FREQUENCY // 5)
    yield mcu
    mcu.close()


@pytest.fixture
def transport(wire: WireMcu):
    mcu = McuTransport(wire.path, disconnect_timeout=_PATIENT)
    mcu.identify(timeout=_PATIENT)
    yield mcu
    mcu.close()


class TestClockSync:
    def test_now_follows_the_mcu_clock_across_the_wrap(self, transport: McuTransport):
        sync = ClockSync(transport, FREQUENCY, interval=0.05)
        sync.start()
        try:
            wait_until(lambda: sync.now() > 2**32 + FREQUENCY // 10)
            before = sync.now()
            message = transport.query("get_clock", response="clock", timeout=_PATIENT)
            after = sync.now()
        finally:
            sync.close()

        clock = sync.extend(int(message.params["clock"]))
        slack = FREQUENCY // 10  # 100 ms
        assert before - slack <= clock <= after + slack

    def test_polls_get_clock_periodically(self, wire: WireMcu, transport: McuTransport):
        sync = ClockSync(transport, FREQUENCY, interval=0.05)
        sync.start()
        try:
            wait_until(
                lambda: [name for name, _ in wire.accepted()].count("get_clock") >= 3
            )
        finally:
            sync.close()

        assert ("get_uptime", {}) in wire.accepted()

    def test_ticks_converts_seconds(self, transport: McuTransport):
        sync = ClockSync(transport, FREQUENCY)

        assert sync.ticks(0.05) == 600_000

    def test_close_stops_the_thread(self, transport: McuTransport):
        sync = ClockSync(transport, FREQUENCY, interval=0.05)
        sync.start()

        sync.close()

        assert "mcu-clock-sync" not in [thread.name for thread in threading.enumerate()]
