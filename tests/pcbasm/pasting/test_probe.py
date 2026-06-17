"""ProbeExecutor のテスト."""

from pcbasm.gcode import GCode
from pcbasm.pasting.probe import ProbeExecutor


class TestProbeExecutor:
    """PROBE 後の退避移動."""

    def test_probe_lifts_by_configured_height(self, mocker):
        klipper = mocker.Mock()
        probe = mocker.Mock()
        stage = mocker.Mock()
        probe.probe.return_value = GCode("PROBE")
        probe.get_last_z_result.return_value = -2.0
        stage.move.return_value = GCode("G1 Z1.25")

        executor = ProbeExecutor(
            klipper=klipper,
            probe=probe,
            stage=stage,
            lift_height=3.25,
            settle_time=0.0,
        )

        assert executor.probe() == -2.0
        stage.move.assert_called_once_with(z=1.25)
