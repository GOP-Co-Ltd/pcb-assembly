import logging

from web.process import setup_logging


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
