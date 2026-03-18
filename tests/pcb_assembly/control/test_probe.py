import pytest

from pcb_assembly.control.probe import ProbeExecutor


class TestProbeExecutor:
    """ProbeExecutorクラスのテスト."""

    @pytest.fixture
    def mock_klipper(self, mocker):
        return mocker.Mock()

    @pytest.fixture
    def mock_probe(self, mocker):
        probe = mocker.Mock()
        probe.get_last_z_result.return_value = -2.5
        return probe

    def test_probe_returns_z_result(self, mock_klipper, mock_probe):
        executor = ProbeExecutor(klipper=mock_klipper, probe=mock_probe)
        z = executor.probe()
        assert z == -2.5

    def test_probe_lifts_z_after_contact(self, mock_klipper, mock_probe):
        executor = ProbeExecutor(
            klipper=mock_klipper, probe=mock_probe, lift_height=3.0
        )
        executor.probe()
        retract_gcode = str(mock_klipper.send_gcode.call_args_list[1][0][0])
        # z = -2.5 + 3.0 = 0.5
        assert "Z0.5" in retract_gcode
