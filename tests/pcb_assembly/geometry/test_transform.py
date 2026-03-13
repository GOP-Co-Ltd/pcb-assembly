import math

import numpy as np
import pytest

from pcb_assembly.geometry.transform import (
    Compose,
    HeightMap,
    Identity,
    Matrix2d,
    Point2d,
    Point3d,
    Rotation,
    Scale,
    Shift,
)


class TestPoint3d:
    """Point3dクラスのテスト."""

    def test_init(self):
        point = Point3d(1.0, 2.0, 3.0)

        assert point.x == 1.0
        assert point.y == 2.0
        assert point.z == 3.0

    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            (Point3d(1.0, 2.0, 3.0), Point3d(4.0, 5.0, 6.0), Point3d(5.0, 7.0, 9.0)),
            (Point3d(0.0, 0.0, 0.0), Point3d(1.0, 1.0, 1.0), Point3d(1.0, 1.0, 1.0)),
            (
                Point3d(-1.0, -2.0, -3.0),
                Point3d(1.0, 2.0, 3.0),
                Point3d(0.0, 0.0, 0.0),
            ),
        ],
    )
    def test_add(self, a, b, expected):
        assert a + b == expected

    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            (Point3d(4.0, 5.0, 6.0), Point3d(1.0, 2.0, 3.0), Point3d(3.0, 3.0, 3.0)),
            (Point3d(1.0, 1.0, 1.0), Point3d(1.0, 1.0, 1.0), Point3d(0.0, 0.0, 0.0)),
            (
                Point3d(0.0, 0.0, 0.0),
                Point3d(1.0, 2.0, 3.0),
                Point3d(-1.0, -2.0, -3.0),
            ),
        ],
    )
    def test_sub(self, a, b, expected):
        assert a - b == expected

    @pytest.mark.parametrize(
        ("point", "scalar", "expected"),
        [
            (Point3d(1.0, 2.0, 3.0), 2.0, Point3d(2.0, 4.0, 6.0)),
            (Point3d(1.0, 2.0, 3.0), 0.5, Point3d(0.5, 1.0, 1.5)),
            (Point3d(1.0, 2.0, 3.0), -1.0, Point3d(-1.0, -2.0, -3.0)),
        ],
    )
    def test_mul(self, point, scalar, expected):
        assert point * scalar == expected

    @pytest.mark.parametrize(
        ("scalar", "point", "expected"),
        [
            (2.0, Point3d(1.0, 2.0, 3.0), Point3d(2.0, 4.0, 6.0)),
            (0.5, Point3d(1.0, 2.0, 3.0), Point3d(0.5, 1.0, 1.5)),
        ],
    )
    def test_rmul(self, scalar, point, expected):
        assert scalar * point == expected

    @pytest.mark.parametrize(
        ("point", "scalar", "expected"),
        [
            (Point3d(2.0, 4.0, 6.0), 2.0, Point3d(1.0, 2.0, 3.0)),
            (Point3d(1.0, 2.0, 3.0), 0.5, Point3d(2.0, 4.0, 6.0)),
        ],
    )
    def test_truediv(self, point, scalar, expected):
        assert point / scalar == expected

    @pytest.mark.parametrize(
        ("point", "expected"),
        [
            (Point3d(3.0, 4.0, 0.0), 5.0),
            (Point3d(0.0, 0.0, 0.0), 0.0),
            (Point3d(1.0, 0.0, 0.0), 1.0),
            (Point3d(1.0, 1.0, 1.0), math.sqrt(3)),
        ],
    )
    def test_norm(self, point, expected):
        assert point.norm() == expected

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({}, Point3d(0.0, 0.0, 0.0)),
            ({"x": 1.0}, Point3d(1.0, 0.0, 0.0)),
            ({"y": 2.0}, Point3d(0.0, 2.0, 0.0)),
            ({"z": 3.0}, Point3d(0.0, 0.0, 3.0)),
            ({"x": 1.0, "y": 2.0}, Point3d(1.0, 2.0, 0.0)),
            ({"x": 1.0, "y": 2.0, "z": 3.0}, Point3d(1.0, 2.0, 3.0)),
        ],
    )
    def test_zero(self, kwargs, expected):
        assert Point3d.zero(**kwargs) == expected

    def test_to2d_ignores_z(self):
        point = Point3d(1.0, 2.0, 3.0)

        result = point.to2d()

        assert result == Point2d(1.0, 2.0)


class TestPoint2d:
    """Point2dクラスのテスト."""

    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            (Point2d(1.0, 2.0), Point2d(3.0, 4.0), Point2d(4.0, 6.0)),
            (Point2d(0.0, 0.0), Point2d(1.0, 1.0), Point2d(1.0, 1.0)),
            (Point2d(-1.0, -2.0), Point2d(1.0, 2.0), Point2d(0.0, 0.0)),
        ],
    )
    def test_add(self, a, b, expected):
        assert a + b == expected

    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            (Point2d(4.0, 6.0), Point2d(1.0, 2.0), Point2d(3.0, 4.0)),
            (Point2d(1.0, 1.0), Point2d(1.0, 1.0), Point2d(0.0, 0.0)),
            (Point2d(0.0, 0.0), Point2d(1.0, 2.0), Point2d(-1.0, -2.0)),
        ],
    )
    def test_sub(self, a, b, expected):
        assert a - b == expected

    @pytest.mark.parametrize(
        ("point", "scalar", "expected"),
        [
            (Point2d(1.0, 2.0), 2.0, Point2d(2.0, 4.0)),
            (Point2d(1.0, 2.0), 0.5, Point2d(0.5, 1.0)),
            (Point2d(1.0, 2.0), -1.0, Point2d(-1.0, -2.0)),
        ],
    )
    def test_mul(self, point, scalar, expected):
        assert point * scalar == expected

    @pytest.mark.parametrize(
        ("scalar", "point", "expected"),
        [
            (2.0, Point2d(1.0, 2.0), Point2d(2.0, 4.0)),
            (0.5, Point2d(1.0, 2.0), Point2d(0.5, 1.0)),
        ],
    )
    def test_rmul(self, scalar, point, expected):
        assert scalar * point == expected

    @pytest.mark.parametrize(
        ("point", "scalar", "expected"),
        [
            (Point2d(2.0, 4.0), 2.0, Point2d(1.0, 2.0)),
            (Point2d(1.0, 2.0), 0.5, Point2d(2.0, 4.0)),
        ],
    )
    def test_truediv(self, point, scalar, expected):
        assert point / scalar == expected

    @pytest.mark.parametrize(
        ("x", "y", "expected_norm"),
        [
            (3.0, 4.0, 5.0),
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 1.0),
            (0.0, 1.0, 1.0),
        ],
    )
    def test_norm_returns_euclidean_distance(
        self, x: float, y: float, expected_norm: float
    ):
        point = Point2d(x=x, y=y)

        assert point.norm == pytest.approx(expected_norm)

    def test_to3d_converts_to_point3d_with_default_z(self):
        point = Point2d(x=1.5, y=2.5)

        result = point.to3d()

        assert result == Point3d(x=1.5, y=2.5, z=0.0)

    def test_to3d_converts_to_point3d_with_specified_z(self):
        point = Point2d(x=1.5, y=2.5)

        result = point.to3d(z=3.0)

        assert result == Point3d(x=1.5, y=2.5, z=3.0)


class TestMatrix2d:
    """Matrix2dクラスのテスト."""

    def test_apply_point2d(self):
        matrix = Matrix2d(np.array([[0.0, -1.0], [1.0, 0.0]]))
        point = Point2d(1.0, 0.0)

        result = matrix.apply(point)

        assert isinstance(result, Point2d)
        assert result.x == pytest.approx(0.0, abs=1e-10)
        assert result.y == pytest.approx(1.0, abs=1e-10)

    def test_apply_point3d_preserves_z(self):
        matrix = Matrix2d(np.array([[2.0, 0.0], [0.0, 3.0]]))
        point = Point3d(1.0, 2.0, 5.0)

        result = matrix.apply(point)

        assert result == Point3d(2.0, 6.0, 5.0)

    def test_inverse(self):
        matrix = Matrix2d(np.array([[2.0, 0.0], [0.0, 4.0]]))
        point = Point2d(3.0, 5.0)

        transformed = matrix.apply(point)
        restored = matrix.inverse().apply(transformed)

        assert restored.x == pytest.approx(point.x, abs=1e-10)
        assert restored.y == pytest.approx(point.y, abs=1e-10)

    def test_invalid_shape_raises(self):
        with pytest.raises(ValueError, match="2x2"):
            Matrix2d(np.array([[1.0, 2.0, 3.0]]))


class TestScale:
    """Scaleクラスのテスト."""

    def test_default_values(self):
        scale = Scale()

        assert scale.x == 1.0
        assert scale.y == 1.0
        assert scale.z == 1.0

    def test_apply_point3d(self):
        scale = Scale(2.0, 3.0, 4.0)
        point = Point3d(1.0, 2.0, 3.0)

        result = scale.apply(point)

        assert result == Point3d(2.0, 6.0, 12.0)

    def test_apply_point2d(self):
        scale = Scale(2.0, 3.0, 4.0)
        point = Point2d(1.0, 2.0)

        result = scale.apply(point)

        assert isinstance(result, Point2d)
        assert result == Point2d(2.0, 6.0)

    def test_inverse(self):
        scale = Scale(2.0, 4.0, 0.5)

        result = scale.inverse()

        assert result == Scale(0.5, 0.25, 2.0)

    @pytest.mark.parametrize(
        ("flip_args", "expected"),
        [
            ({"x": True}, Scale(-1.0, 1.0, 1.0)),
            ({"y": True}, Scale(1.0, -1.0, 1.0)),
            ({"z": True}, Scale(1.0, 1.0, -1.0)),
            ({"x": True, "y": True}, Scale(-1.0, -1.0, 1.0)),
            ({"x": True, "y": True, "z": True}, Scale(-1.0, -1.0, -1.0)),
        ],
    )
    def test_flip(self, flip_args, expected):
        result = Scale.flip(**flip_args)

        assert result == expected


class TestIdentity:
    """Identityクラスのテスト."""

    def test_apply_point3d(self):
        identity = Identity()
        point = Point3d(1.0, 2.0, 3.0)

        result = identity.apply(point)

        assert result == point

    def test_apply_point2d(self):
        identity = Identity()
        point = Point2d(1.0, 2.0)

        result = identity.apply(point)

        assert result == point

    def test_inverse(self):
        identity = Identity()

        result = identity.inverse()

        assert result is identity


class TestRotation:
    """Rotationクラスのテスト."""

    def test_default_value(self):
        rotation = Rotation()

        assert rotation.degrees == 0.0

    @pytest.mark.parametrize(
        ("degrees", "expected_radians"),
        [
            (0.0, 0.0),
            (90.0, math.pi / 2),
            (180.0, math.pi),
            (-90.0, -math.pi / 2),
        ],
    )
    def test_radians(self, degrees, expected_radians):
        rotation = Rotation(degrees)

        assert rotation.radians == pytest.approx(expected_radians)

    def test_apply_point3d(self):
        rotation = Rotation(90.0)
        point = Point3d(1.0, 0.0, 5.0)

        result = rotation.apply(point)

        assert result.x == pytest.approx(0.0, abs=1e-10)
        assert result.y == pytest.approx(1.0, abs=1e-10)
        assert result.z == pytest.approx(5.0, abs=1e-10)

    def test_apply_point2d(self):
        rotation = Rotation(90.0)
        point = Point2d(1.0, 0.0)

        result = rotation.apply(point)

        assert isinstance(result, Point2d)
        assert result.x == pytest.approx(0.0, abs=1e-10)
        assert result.y == pytest.approx(1.0, abs=1e-10)

    def test_inverse(self):
        rotation = Rotation(45.0)

        result = rotation.inverse()

        assert result == Rotation(-45.0)

    @pytest.mark.parametrize(
        ("base", "target", "expected_degrees"),
        [
            (Point2d(1.0, 0.0), Point2d(1.0, 0.0), 0.0),
            (Point2d(1.0, 0.0), Point2d(0.0, 1.0), 90.0),
            (Point2d(1.0, 0.0), Point2d(0.0, -1.0), -90.0),
            (Point2d(1.0, 0.0), Point2d(-1.0, 0.0), 180.0),
            (Point2d(1.0, 0.0), Point2d(1.0, 1.0), 45.0),
            (Point2d(0.0, 1.0), Point2d(1.0, 0.0), -90.0),
        ],
    )
    def test_from_points(self, base, target, expected_degrees):
        result = Rotation.from_points(base, target)

        assert result.degrees == pytest.approx(expected_degrees)


class TestShift:
    """Shiftクラスのテスト."""

    def test_default_values(self):
        translation = Shift()

        assert translation.x == 0.0
        assert translation.y == 0.0
        assert translation.z == 0.0

    def test_apply_point3d(self):
        translation = Shift(10.0, 20.0, 30.0)
        point = Point3d(1.0, 2.0, 3.0)

        result = translation.apply(point)

        assert result == Point3d(11.0, 22.0, 33.0)

    def test_apply_point2d(self):
        translation = Shift(10.0, 20.0, 30.0)
        point = Point2d(1.0, 2.0)

        result = translation.apply(point)

        assert isinstance(result, Point2d)
        assert result == Point2d(11.0, 22.0)

    def test_inverse(self):
        translation = Shift(10.0, 20.0, 30.0)

        result = translation.inverse()

        assert result == Shift(-10.0, -20.0, -30.0)

    def test_from_point_point3d(self):
        point = Point3d(10.0, 20.0, 30.0)

        result = Shift.from_point(point)

        assert result == Shift(10.0, 20.0, 30.0)

    def test_from_point_point2d(self):
        point = Point2d(10.0, 20.0)

        result = Shift.from_point(point)

        assert result == Shift(10.0, 20.0, 0.0)


class TestCompose:
    """Composeクラスのテスト."""

    def test_empty_compose_returns_same_point(self):
        compose = Compose()
        point = Point3d(1.0, 2.0, 3.0)

        result = compose.apply(point)

        assert result == point

    def test_apply_single_transform(self):
        compose = Compose([Scale(2.0, 3.0, 4.0)])
        point = Point3d(1.0, 2.0, 3.0)

        result = compose.apply(point)

        assert result == Point3d(2.0, 6.0, 12.0)

    def test_apply_multiple_transforms_in_order(self):
        # Scale(2,2,1) → Rotation(90°) → Shift(10,0,0)
        compose = Compose(
            [
                Scale(2.0, 2.0, 1.0),
                Rotation(90.0),
                Shift(10.0, 0.0, 0.0),
            ]
        )
        point = Point3d(1.0, 0.0, 0.0)

        result = compose.apply(point)

        # (1,0,0) → scale → (2,0,0) → rotate 90° → (0,2,0) → translate → (10,2,0)
        assert result.x == pytest.approx(10.0, abs=1e-10)
        assert result.y == pytest.approx(2.0, abs=1e-10)
        assert result.z == pytest.approx(0.0, abs=1e-10)

    def test_apply_point2d(self):
        compose = Compose(
            [
                Scale(2.0, 2.0, 1.0),
                Rotation(90.0),
                Shift(10.0, 0.0, 0.0),
            ]
        )
        point = Point2d(1.0, 0.0)

        result = compose.apply(point)

        assert isinstance(result, Point2d)
        assert result.x == pytest.approx(10.0, abs=1e-10)
        assert result.y == pytest.approx(2.0, abs=1e-10)

    def test_inverse(self):
        compose = Compose(
            [
                Scale(2.0, 2.0, 1.0),
                Rotation(90.0),
                Shift(10.0, 5.0, 0.0),
            ]
        )
        point = Point3d(1.0, 2.0, 3.0)

        transformed = compose.apply(point)
        restored = compose.inverse().apply(transformed)

        assert restored.x == pytest.approx(point.x, abs=1e-10)
        assert restored.y == pytest.approx(point.y, abs=1e-10)
        assert restored.z == pytest.approx(point.z, abs=1e-10)

    def test_inverse_order_is_reversed(self):
        compose = Compose(
            [
                Scale(2.0, 1.0, 1.0),
                Shift(10.0, 0.0, 0.0),
            ]
        )

        inverse = compose.inverse()

        # inverse should be: Shift(-10, 0, 0) → Scale(0.5, 1, 1)
        assert len(inverse) == 2
        assert isinstance(inverse[0], Shift)
        assert isinstance(inverse[1], Scale)
        assert inverse[0] == Shift(-10.0, 0.0, 0.0)
        assert inverse[1] == Scale(0.5, 1.0, 1.0)


class TestHeightMap:
    """HeightMapクラスのテスト."""

    @pytest.fixture
    def height_map(self):
        # 2x2 grid over (0,0)-(10,20) with Z values:
        # [[1.0, 2.0],
        #  [3.0, 4.0]]
        z_values = np.array([[1.0, 2.0], [3.0, 4.0]])
        return HeightMap(
            z_values=z_values, x_min=0.0, x_max=10.0, y_min=0.0, y_max=20.0
        )

    def test_apply_point3d_adds_interpolated_z(self, height_map):
        point = Point3d(0.0, 0.0, 10.0)

        result = height_map.apply(point)

        assert result.x == 0.0
        assert result.y == 0.0
        assert result.z == pytest.approx(11.0)

    def test_apply_point2d_returns_unchanged(self, height_map):
        point = Point2d(5.0, 10.0)

        result = height_map.apply(point)

        assert result == point
        assert isinstance(result, Point2d)

    @pytest.mark.parametrize(
        ("x", "y", "expected_z_offset"),
        [
            (0.0, 0.0, 1.0),  # top-left
            (10.0, 0.0, 2.0),  # top-right
            (0.0, 20.0, 3.0),  # bottom-left
            (10.0, 20.0, 4.0),  # bottom-right
        ],
    )
    def test_corner_values(self, height_map, x, y, expected_z_offset):
        result = height_map.apply(Point3d(x, y, 0.0))

        assert result.z == pytest.approx(expected_z_offset)

    def test_center_interpolation(self, height_map):
        # Center of 2x2 grid should be average of all 4 corners
        result = height_map.apply(Point3d(5.0, 10.0, 0.0))

        assert result.z == pytest.approx(2.5)

    @pytest.mark.parametrize(
        ("x", "y", "expected_z_offset"),
        [
            (-5.0, 0.0, 1.0),  # clamped to x_min
            (15.0, 0.0, 2.0),  # clamped to x_max
            (0.0, -5.0, 1.0),  # clamped to y_min
            (0.0, 25.0, 3.0),  # clamped to y_max
        ],
    )
    def test_clamp_outside_grid(self, height_map, x, y, expected_z_offset):
        result = height_map.apply(Point3d(x, y, 0.0))

        assert result.z == pytest.approx(expected_z_offset)

    def test_inverse_roundtrip(self, height_map):
        point = Point3d(5.0, 10.0, 100.0)
        transformed = height_map.apply(point)
        restored = height_map.inverse().apply(transformed)
        assert restored.z == pytest.approx(point.z)

    def test_save_load_roundtrip(self, height_map, tmp_path):
        path = tmp_path / "heightmap.json"
        height_map.save(path)

        loaded = HeightMap.load(path)

        assert np.array_equal(loaded.z_values, height_map.z_values)
        assert loaded.x_min == height_map.x_min
        assert loaded.x_max == height_map.x_max
        assert loaded.y_min == height_map.y_min
        assert loaded.y_max == height_map.y_max

    def test_1d_array_raises_value_error(self):
        with pytest.raises(ValueError, match="2次元配列"):
            HeightMap(
                z_values=np.array([1.0, 2.0]),
                x_min=0.0,
                x_max=10.0,
                y_min=0.0,
                y_max=20.0,
            )

    def test_x_min_ge_x_max_raises_value_error(self):
        with pytest.raises(ValueError, match="x_minはx_maxより小さい"):
            HeightMap(
                z_values=np.array([[1.0, 2.0], [3.0, 4.0]]),
                x_min=10.0,
                x_max=10.0,
                y_min=0.0,
                y_max=20.0,
            )

    def test_y_min_ge_y_max_raises_value_error(self):
        with pytest.raises(ValueError, match="y_minはy_maxより小さい"):
            HeightMap(
                z_values=np.array([[1.0, 2.0], [3.0, 4.0]]),
                x_min=0.0,
                x_max=10.0,
                y_min=20.0,
                y_max=0.0,
            )

    def test_shape_less_than_2x2_raises_value_error(self):
        with pytest.raises(ValueError, match="2x2以上"):
            HeightMap(
                z_values=np.array([[1.0]]),
                x_min=0.0,
                x_max=10.0,
                y_min=0.0,
                y_max=20.0,
            )
