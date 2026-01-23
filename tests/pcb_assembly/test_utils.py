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
        setup_logging()
        logger = logging.getLogger("test_stdout")
        logger.info("test message")

        captured = capsys.readouterr()
        assert "test message" in captured.out

    def test_custom_level(self, capsys):
        setup_logging(level=logging.WARNING)
        logger = logging.getLogger("test_level")
        logger.info("info message")
        logger.warning("warning message")

        captured = capsys.readouterr()
        assert "info message" not in captured.out
        assert "warning message" in captured.out
