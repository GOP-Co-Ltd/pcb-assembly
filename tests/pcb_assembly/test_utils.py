import logging
import sys

from pcb_assembly.utils import get_class_module_path, setup_logging


class _SampleClass:
    """テスト専用のクラス."""

    pass


class TestGetClassModulePath:
    """get_class_module_path関数のテスト."""

    def test_returns_module_path(self):
        result = get_class_module_path(_SampleClass)

        assert result == "tests.pcb_assembly.test_utils._SampleClass"

    def test_builtin_class(self):
        result = get_class_module_path(int)

        assert result == "builtins.int"


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

    def test_custom_namespaces(self, capsys):
        setup_logging(namespaces=["custom_ns1", "custom_ns2"])
        logger1 = logging.getLogger("custom_ns1")
        logger2 = logging.getLogger("custom_ns2")
        logger1.info("message from ns1")
        logger2.info("message from ns2")

        captured = capsys.readouterr()
        assert "message from ns1" in captured.out
        assert "message from ns2" in captured.out

    def test_unregistered_namespace_not_logged(self, capsys):
        setup_logging(namespaces=["registered_ns"])
        unregistered_logger = logging.getLogger("unregistered_ns")
        unregistered_logger.info("should not appear")

        captured = capsys.readouterr()
        assert "should not appear" not in captured.out
