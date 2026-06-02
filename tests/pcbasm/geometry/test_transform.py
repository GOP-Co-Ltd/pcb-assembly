import math

import numpy as np
import pytest

from pcbasm.geometry.transform import (
    Compose,
    HeightPlane,
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


class TestHeightPlane:
    """HeightPlaneクラスのテスト."""

    @pytest.fixture
    def triangle_points(self):
        # 平面 z = 0.1x + 0.2y の3点
        return (
            Point3d(0.0, 0.0, 0.0),
            Point3d(10.0, 0.0, 1.0),
            Point3d(0.0, 10.0, 2.0),
        )

    @pytest.fixture
    def height_plane(self, triangle_points):
        return HeightPlane(points=triangle_points)

    def test_apply_point3d_evaluates_plane(self, height_plane):
        # 平面式 z = 0.1x + 0.2y より (5, 5) では z = 0.1*5 + 0.2*5 = 1.5
        result = height_plane.apply(Point3d(5.0, 5.0, 10.0))

        assert result.x == 5.0
        assert result.y == 5.0
        assert result.z == pytest.approx(11.5)

    @pytest.mark.parametrize(
        ("x", "y", "expected_z_offset"),
        [
            (0.0, 0.0, 0.0),
            (10.0, 0.0, 1.0),
            (0.0, 10.0, 2.0),
            (5.0, 0.0, 0.5),  # 辺上
            (0.0, 5.0, 1.0),  # 辺上
        ],
    )
    def test_corner_and_edge_values(self, height_plane, x, y, expected_z_offset):
        result = height_plane.apply(Point3d(x, y, 0.0))

        assert result.z == pytest.approx(expected_z_offset)

    def test_apply_with_four_points(self):
        # 長方形の4頂点で平面 z = 0.1x + 0.2y
        points = (
            Point3d(0.0, 0.0, 0.0),
            Point3d(10.0, 0.0, 1.0),
            Point3d(0.0, 10.0, 2.0),
            Point3d(10.0, 10.0, 3.0),
        )
        hp = HeightPlane(points=points)

        # 中心 (5,5) の期待値は 0.5 + 1.0 = 1.5
        result = hp.apply(Point3d(5.0, 5.0, 0.0))

        assert result.z == pytest.approx(1.5)

    def test_outside_sample_extent_extrapolates_planarly(self, height_plane):
        # 平面 z = 0.1x + 0.2y を全域に外挿。(100, 100) では 0.1*100 + 0.2*100 = 30.0
        result = height_plane.apply(Point3d(100.0, 100.0, 0.0))

        assert result.x == 100.0
        assert result.y == 100.0
        assert result.z == pytest.approx(30.0)

    def test_apply_point2d_returns_unchanged(self, height_plane):
        point = Point2d(5.0, 5.0)

        result = height_plane.apply(point)

        assert isinstance(result, Point2d)
        assert result == point

    def test_inverse_roundtrip(self, height_plane):
        point = Point3d(5.0, 5.0, 100.0)

        transformed = height_plane.apply(point)
        restored = height_plane.inverse().apply(transformed)

        assert restored.x == pytest.approx(point.x)
        assert restored.y == pytest.approx(point.y)
        assert restored.z == pytest.approx(point.z)

    @pytest.mark.parametrize(
        "points",
        [
            (),
            (Point3d(0.0, 0.0, 0.0),),
            (Point3d(0.0, 0.0, 0.0), Point3d(1.0, 1.0, 1.0)),
        ],
    )
    def test_fewer_than_three_points_raises_value_error(self, points):
        with pytest.raises(ValueError, match="3点以上"):
            HeightPlane(points=points)

    def test_collinear_points_raises_value_error(self):
        points = (
            Point3d(0.0, 0.0, 0.0),
            Point3d(1.0, 0.0, 1.0),
            Point3d(2.0, 0.0, 2.0),
        )

        with pytest.raises(ValueError, match="同一直線上"):
            HeightPlane(points=points)

    def test_collinear_four_points_raises_value_error(self):
        # 4点でも共線ならNG
        points = (
            Point3d(0.0, 0.0, 0.0),
            Point3d(1.0, 1.0, 1.0),
            Point3d(2.0, 2.0, 2.0),
            Point3d(3.0, 3.0, 3.0),
        )

        with pytest.raises(ValueError, match="同一直線上"):
            HeightPlane(points=points)

    def test_least_squares_fits_noisy_points(self):
        rng = np.random.default_rng(seed=42)
        true_a, true_b, true_c = 0.1, 0.2, 0.3
        xys = [
            (0.0, 0.0),
            (10.0, 0.0),
            (0.0, 10.0),
            (10.0, 10.0),
            (5.0, 5.0),
            (5.0, 0.0),
            (2.0, 8.0),
            (8.0, 2.0),
        ]
        noise = rng.normal(0.0, 0.01, size=len(xys))
        points = tuple(
            Point3d(x, y, true_a * x + true_b * y + true_c + n)
            for (x, y), n in zip(xys, noise, strict=True)
        )
        hp = HeightPlane(points=points)

        result = hp.apply(Point3d(5.0, 5.0, 0.0))

        assert result.z == pytest.approx(true_a * 5 + true_b * 5 + true_c, abs=0.05)
