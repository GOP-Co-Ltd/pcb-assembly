import math

import numpy as np
import pytest

from pcb_assembly.geometry.transform import Point2d, Point3d, Rotation, Scale, Transform


class TestPoint3d:
    """Point3dクラスのテスト."""

    def test_init(self):
        point = Point3d(1.0, 2.0, 3.0)

        assert point.x == 1.0
        assert point.y == 2.0
        assert point.z == 3.0

    def test_numpy(self):
        point = Point3d(1.0, 2.0, 3.0)

        result = point.numpy()

        assert result.dtype == np.float64
        assert np.array_equal(result, np.array([1.0, 2.0, 3.0]))

    def test_from_numpy(self):
        array = np.array([1.0, 2.0, 3.0])

        point = Point3d.from_numpy(array)

        assert point == Point3d(1.0, 2.0, 3.0)

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
            Point3d.from_numpy(array)

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


class TestTransform:
    """Transformクラスのテスト."""

    def test_default_values(self):
        transform = Transform()

        assert transform.scale == Scale()
        assert transform.rotation == Rotation()
        assert transform.translation == Point3d(0.0, 0.0, 0.0)

    def test_apply_translation_only(self):
        transform = Transform(translation=Point3d(10.0, 20.0, 30.0))
        point = Point3d(1.0, 2.0, 3.0)

        result = transform.apply(point)

        assert result == Point3d(11.0, 22.0, 33.0)

    def test_apply_scale_only(self):
        transform = Transform(scale=Scale(2.0, 3.0, 4.0))
        point = Point3d(1.0, 2.0, 3.0)

        result = transform.apply(point)

        assert result == Point3d(2.0, 6.0, 12.0)

    def test_apply_rotation_only(self):
        transform = Transform(rotation=Rotation(90.0))
        point = Point3d(1.0, 0.0, 0.0)

        result = transform.apply(point)

        assert np.isclose(result.x, 0.0, atol=1e-10)
        assert np.isclose(result.y, 1.0, atol=1e-10)
        assert np.isclose(result.z, 0.0, atol=1e-10)

    def test_apply_combined(self):
        # Scale(2,2,1) → Rotation(90°) → Translation(10,0,0)
        transform = Transform(
            scale=Scale(2.0, 2.0, 1.0),
            rotation=Rotation(90.0),
            translation=Point3d(10.0, 0.0, 0.0),
        )
        point = Point3d(1.0, 0.0, 0.0)

        result = transform.apply(point)

        # (1,0,0) → scale → (2,0,0) → rotate 90° → (0,2,0) → translate → (10,2,0)
        assert np.isclose(result.x, 10.0, atol=1e-10)
        assert np.isclose(result.y, 2.0, atol=1e-10)
        assert np.isclose(result.z, 0.0, atol=1e-10)

    def test_apply_point2d_returns_point2d(self):
        transform = Transform(translation=Point3d(10.0, 20.0, 30.0))
        point = Point2d(1.0, 2.0)

        result = transform.apply(point)

        assert isinstance(result, Point2d)
        assert result == Point2d(11.0, 22.0)

    def test_apply_point2d_with_rotation(self):
        transform = Transform(rotation=Rotation(90.0))
        point = Point2d(1.0, 0.0)

        result = transform.apply(point)

        assert isinstance(result, Point2d)
        assert np.isclose(result.x, 0.0, atol=1e-10)
        assert np.isclose(result.y, 1.0, atol=1e-10)

    def test_inverse(self):
        transform = Transform(
            scale=Scale(2.0, 2.0, 1.0),
            rotation=Rotation(90.0),
            translation=Point3d(10.0, 5.0, 0.0),
        )
        point = Point3d(1.0, 2.0, 3.0)

        transformed = transform.apply(point)
        restored = transform.inverse().apply(transformed)

        assert np.isclose(restored.x, point.x, atol=1e-10)
        assert np.isclose(restored.y, point.y, atol=1e-10)
        assert np.isclose(restored.z, point.z, atol=1e-10)

    @pytest.mark.parametrize(
        "points",
        [
            [],
            [Point3d(1.0, 2.0, 3.0)],
            [Point3d(1.0, 0.0, 0.0), Point3d(0.0, 1.0, 0.0), Point3d(1.0, 1.0, 5.0)],
        ],
    )
    def test_batch(self, points):
        transform = Transform(
            scale=Scale(2.0, 3.0, 1.0),
            rotation=Rotation(45.0),
            translation=Point3d(5.0, -3.0, 2.0),
        )

        batch_results = transform.batch(points)
        apply_results = [transform.apply(p) for p in points]

        assert len(batch_results) == len(apply_results)
        for batch_res, apply_res in zip(batch_results, apply_results, strict=True):
            assert np.isclose(batch_res.x, apply_res.x, atol=1e-10)
            assert np.isclose(batch_res.y, apply_res.y, atol=1e-10)
            assert np.isclose(batch_res.z, apply_res.z, atol=1e-10)
