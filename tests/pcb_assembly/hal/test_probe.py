from unittest.mock import MagicMock

import pytest

from pcb_assembly.hal.probe import ProbeSensor


class TestProbeSensor:
    """ProbeSensorクラスのテスト."""

    def test_init_raises_when_no_probe_config(self):
        mock_klipper = MagicMock()
        mock_klipper.get_config.return_value = {}

        with pytest.raises(RuntimeError, match="printer.cfgに\\[probe\\]セクション"):
            ProbeSensor(mock_klipper)

    def test_init_succeeds_with_probe_config(self):
        mock_klipper = MagicMock()
        mock_klipper.get_config.return_value = {"probe": {"pin": "^!PA1"}}

        probe = ProbeSensor(mock_klipper)
        assert probe is not None

    def test_get_last_z_result(self):
        mock_klipper = MagicMock()
        mock_klipper.get_config.return_value = {"probe": {"pin": "^!PA1"}}
        mock_klipper.get_status.return_value = -5.123

        probe = ProbeSensor(mock_klipper)
        result = probe.get_last_z_result()

        assert result == -5.123
