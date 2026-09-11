import logging
import sys

import pytest

from pcbasm.utils import get_class_module_path, is_finite_number, setup_logging


class _SampleClass:
    """テスト専用のクラス."""

    pass


class TestGetClassModulePath:
    """get_class_module_path関数のテスト."""

    @pytest.mark.parametrize(
        ("cls", "expected"),
        [
            (_SampleClass, "tests.pcbasm.test_utils._SampleClass"),
            (int, "builtins.int"),
        ],
        ids=["project-class", "builtin-class"],
    )
    def test_returns_module_path(self, cls: type, expected: str):
        assert get_class_module_path(cls) == expected


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
