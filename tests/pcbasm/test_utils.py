import logging

import pytest

from pcbasm.utils import is_finite_number, setup_logging


class TestSetupLogging:
    """setup_logging関数のテスト."""

    def test_outputs_to_stdout(self, capsys):
        setup_logging(namespaces=["test_stdout"])
        logger = logging.getLogger("test_stdout")
        logger.info("test message")

        captured = capsys.readouterr()
        assert "test message" in captured.out

    def test_custom_level(self, capsys):
        setup_logging(level=logging.WARNING, namespaces=["test_level"])
        logger = logging.getLogger("test_level")
        logger.info("info message")
        logger.warning("warning message")

        captured = capsys.readouterr()
        assert "info message" not in captured.out
        assert "warning message" in captured.out

    def test_unregistered_namespace_not_logged(self, capsys):
        setup_logging(namespaces=["registered_ns"])
        unregistered_logger = logging.getLogger("unregistered_ns")
        unregistered_logger.info("should not appear")

        captured = capsys.readouterr()
        assert "should not appear" not in captured.out


class TestIsFiniteNumber:
    @pytest.mark.parametrize("value", [0, 1, -3, 0.5, -2.25, 10**300])
    def test_accepts_finite_numbers(self, value: object):
        assert is_finite_number(value) is True

    @pytest.mark.parametrize(
        "value",
        [True, False, None, "1", float("inf"), float("-inf"), float("nan"), 10**400],
    )
    def test_rejects_bool_non_numbers_and_non_finite(self, value: object):
        assert is_finite_number(value) is False
