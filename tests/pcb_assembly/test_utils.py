from pcb_assembly.utils import get_class_module_path


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
