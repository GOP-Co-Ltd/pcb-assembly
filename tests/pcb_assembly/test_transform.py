import numpy as np
import pytest

from pcb_assembly.transform import Position, Rotation, Scale


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


class TestScale:
    """Scaleクラスのテスト."""

    def test_default_values(self):
        scale = Scale()

        assert scale.x == 1.0
        assert scale.y == 1.0
        assert scale.z == 1.0

    def test_to_matrix(self):
        scale = Scale(2.0, 3.0, 4.0)

        result = scale.to_matrix()

        expected = np.array([[2.0, 0, 0], [0, 3.0, 0], [0, 0, 4.0]])
        assert result.dtype == np.float64
        assert np.array_equal(result, expected)

    def test_inverse(self):
        scale = Scale(2.0, 4.0, 0.5)

        result = scale.inverse()

        assert result == Scale(0.5, 0.25, 2.0)

    @pytest.mark.parametrize(
        ("flip_args", "expected"),
        [
            ({"x": True}, Scale(-2.0, 3.0, 4.0)),
            ({"y": True}, Scale(2.0, -3.0, 4.0)),
            ({"z": True}, Scale(2.0, 3.0, -4.0)),
            ({"x": True, "y": True}, Scale(-2.0, -3.0, 4.0)),
            ({"x": True, "y": True, "z": True}, Scale(-2.0, -3.0, -4.0)),
        ],
    )
    def test_flip(self, flip_args, expected):
        scale = Scale(2.0, 3.0, 4.0)

        result = scale.flip(**flip_args)

        assert result == expected


class TestRotation:
    """Rotationクラスのテスト."""

    def test_default_value(self):
        rotation = Rotation()

        assert rotation.degrees == 0.0

    @pytest.mark.parametrize(
        ("degrees", "expected_radians"),
        [
            (0.0, 0.0),
            (90.0, np.pi / 2),
            (180.0, np.pi),
            (-90.0, -np.pi / 2),
        ],
    )
    def test_radians(self, degrees, expected_radians):
        rotation = Rotation(degrees)

        assert np.isclose(rotation.radians, expected_radians)

    @pytest.mark.parametrize(
        ("degrees", "expected_matrix"),
        [
            (0.0, np.eye(3)),
            (90.0, np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]], dtype=np.float64)),
            (180.0, np.array([[-1, 0, 0], [0, -1, 0], [0, 0, 1]], dtype=np.float64)),
        ],
    )
    def test_to_matrix(self, degrees, expected_matrix):
        rotation = Rotation(degrees)

        result = rotation.to_matrix()

        assert result.dtype == np.float64
        assert np.allclose(result, expected_matrix)

    def test_inverse(self):
        rotation = Rotation(45.0)

        result = rotation.inverse()

        assert result == Rotation(-45.0)
