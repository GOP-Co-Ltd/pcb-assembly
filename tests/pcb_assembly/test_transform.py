import numpy as np
import pytest

from pcb_assembly.transform import Position


class TestPosition:
    """Positionクラスのテスト."""

    def test_init(self):
        pos = Position(1.0, 2.0, 3.0)

        assert pos.x == 1.0
        assert pos.y == 2.0
        assert pos.z == 3.0

    def test_numpy(self):
        pos = Position(1.0, 2.0, 3.0)

        result = pos.numpy()

        assert result.dtype == np.float64
        assert np.array_equal(result, np.array([1.0, 2.0, 3.0]))

    def test_from_numpy(self):
        array = np.array([1.0, 2.0, 3.0])

        pos = Position.from_numpy(array)

        assert pos == Position(1.0, 2.0, 3.0)

    @pytest.mark.parametrize(
        "array",
        [
            np.array([1.0, 2.0]),
            np.array([1.0, 2.0, 3.0, 4.0]),
            np.array([[1.0, 2.0, 3.0]]),
        ],
    )
    def test_from_numpy_invalid_shape(self, array):
        with pytest.raises(ValueError, match=r"配列の形状は\(3,\)である必要があります"):
            Position.from_numpy(array)
