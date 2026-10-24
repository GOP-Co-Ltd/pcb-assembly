import pytest

from pcbasm.utils import is_finite_number


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
