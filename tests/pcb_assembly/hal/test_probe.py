import pytest

from pcb_assembly.hal.probe import Probe, ProbeResult


class TestProbeResult:
    """ProbeResultクラスのテスト."""

    def test_has_min_and_max(self):
        result = ProbeResult(min=-1.5, max=2.5)

        assert result.min == -1.5
        assert result.max == 2.5


class TestProbe:
    """Probeクラスのテスト."""

    def test_start_and_stop(self, mock_probe_backend):
        probe = Probe(a_pin=17, b_pin=27, rotation_distance=40.0)

        probe.start()
        probe.stop()

        result = probe.result()
        assert result.min == 0.0
        assert result.max == 0.0

    def test_start_raises_when_already_measuring(self, mock_probe_backend):
        probe = Probe(a_pin=17, b_pin=27, rotation_distance=40.0)
        probe.start()

        with pytest.raises(RuntimeError, match="計測中です"):
            probe.start()

    def test_stop_raises_when_not_measuring(self, mock_probe_backend):
        probe = Probe(a_pin=17, b_pin=27, rotation_distance=40.0)

        with pytest.raises(RuntimeError, match="計測中ではありません"):
            probe.stop()

    def test_result_raises_when_measuring(self, mock_probe_backend):
        probe = Probe(a_pin=17, b_pin=27, rotation_distance=40.0)
        probe.start()

        with pytest.raises(RuntimeError, match="計測中です"):
            probe.result()

    def test_result_raises_when_not_measured(self, mock_probe_backend):
        probe = Probe(a_pin=17, b_pin=27, rotation_distance=40.0)

        with pytest.raises(RuntimeError, match="計測が行われていません"):
            probe.result()

    def test_context_manager(self, mock_probe_backend):
        probe = Probe(a_pin=17, b_pin=27, rotation_distance=40.0)

        with probe:
            pass

        result = probe.result()
        assert result.min == 0.0
        assert result.max == 0.0
