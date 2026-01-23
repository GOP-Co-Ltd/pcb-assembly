import math

import pytest

from pcb_assembly.geometry.transform import (
    Compose,
    Point2d,
    Point3d,
    Rotation,
    Scale,
    Translation,
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


class TestTranslation:
    """Translationクラスのテスト."""

    def test_default_values(self):
        translation = Translation()

        assert translation.x == 0.0
        assert translation.y == 0.0
        assert translation.z == 0.0

    def test_apply_point3d(self):
        translation = Translation(10.0, 20.0, 30.0)
        point = Point3d(1.0, 2.0, 3.0)

        result = translation.apply(point)

        assert result == Point3d(11.0, 22.0, 33.0)

    def test_apply_point2d(self):
        translation = Translation(10.0, 20.0, 30.0)
        point = Point2d(1.0, 2.0)

        result = translation.apply(point)

        assert isinstance(result, Point2d)
        assert result == Point2d(11.0, 22.0)

    def test_inverse(self):
        translation = Translation(10.0, 20.0, 30.0)

        result = translation.inverse()

        assert result == Translation(-10.0, -20.0, -30.0)


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
        # Scale(2,2,1) → Rotation(90°) → Translation(10,0,0)
        compose = Compose(
            [
                Scale(2.0, 2.0, 1.0),
                Rotation(90.0),
                Translation(10.0, 0.0, 0.0),
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
                Translation(10.0, 0.0, 0.0),
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
                Translation(10.0, 5.0, 0.0),
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
                Translation(10.0, 0.0, 0.0),
            ]
        )

        inverse = compose.inverse()

        # inverse should be: Translation(-10, 0, 0) → Scale(0.5, 1, 1)
        assert len(inverse) == 2
        assert isinstance(inverse[0], Translation)
        assert isinstance(inverse[1], Scale)
        assert inverse[0] == Translation(-10.0, 0.0, 0.0)
        assert inverse[1] == Scale(0.5, 1.0, 1.0)
