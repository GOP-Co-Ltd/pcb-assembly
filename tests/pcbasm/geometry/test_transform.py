import math

import numpy as np
import pytest

from pcbasm.geometry.transform import (
    Compose,
    HeightPlane,
    Matrix2d,
    Point2d,
    Point3d,
    Rotation,
    Scale,
    Shift,
)


class TestPoint3d:
    """Point3dクラスのテスト."""

    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            (Point3d(1.0, 2.0, 3.0), Point3d(4.0, 5.0, 6.0), Point3d(5.0, 7.0, 9.0)),
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
            (Point3d(1.0, 2.0, 3.0), -1.0, Point3d(-1.0, -2.0, -3.0)),
        ],
    )
    def test_mul(self, point, scalar, expected):
        assert point * scalar == expected

    @pytest.mark.parametrize(
        ("scalar", "point", "expected"),
        [
            (2.0, Point3d(1.0, 2.0, 3.0), Point3d(2.0, 4.0, 6.0)),
        ],
    )
    def test_rmul(self, scalar, point, expected):
        assert scalar * point == expected

    @pytest.mark.parametrize(
        ("point", "scalar", "expected"),
        [
            (Point3d(2.0, 4.0, 6.0), 2.0, Point3d(1.0, 2.0, 3.0)),
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
            (Point2d(-1.0, -2.0), Point2d(1.0, 2.0), Point2d(0.0, 0.0)),
        ],
    )
    def test_add(self, a, b, expected):
        assert a + b == expected

    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            (Point2d(4.0, 6.0), Point2d(1.0, 2.0), Point2d(3.0, 4.0)),
            (Point2d(0.0, 0.0), Point2d(1.0, 2.0), Point2d(-1.0, -2.0)),
        ],
    )
    def test_sub(self, a, b, expected):
        assert a - b == expected

    @pytest.mark.parametrize(
        ("point", "scalar", "expected"),
        [
            (Point2d(1.0, 2.0), 2.0, Point2d(2.0, 4.0)),
            (Point2d(1.0, 2.0), -1.0, Point2d(-1.0, -2.0)),
        ],
    )
    def test_mul(self, point, scalar, expected):
        assert point * scalar == expected

    @pytest.mark.parametrize(
        ("scalar", "point", "expected"),
        [
            (2.0, Point2d(1.0, 2.0), Point2d(2.0, 4.0)),
        ],
    )
    def test_rmul(self, scalar, point, expected):
        assert scalar * point == expected

    @pytest.mark.parametrize(
        ("point", "scalar", "expected"),
        [
            (Point2d(2.0, 4.0), 2.0, Point2d(1.0, 2.0)),
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
            ({"x": True, "y": True, "z": True}, Scale(-1.0, -1.0, -1.0)),
        ],
    )
    def test_flip(self, flip_args, expected):
        result = Scale.flip(**flip_args)

        assert result == expected


class TestRotation:
    """Rotationクラスのテスト."""

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

    @pytest.mark.parametrize(
        ("point", "expected"),
        [
            (Point3d(10.0, 20.0, 30.0), Shift(10.0, 20.0, 30.0)),
            (Point2d(10.0, 20.0), Shift(10.0, 20.0, 0.0)),
        ],
        ids=["point3d", "point2d-defaults-z-to-zero"],
    )
    def test_from_point(self, point, expected):
        assert Shift.from_point(point) == expected


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


def _quadratic_z(x, y, c, a, b, d, e, f):
    """2次曲面 z = c + ax + by + dx² + ey² + fxy を評価する補助関数."""
    return c + a * x + b * y + d * x * x + e * y * y + f * x * y


class TestHeightPlane:
    """HeightPlaneクラスのテスト.

    HeightPlane は計測点に z = c + ax + by + dx² + ey² + fxy の2次曲面を最小二乗で
    フィットし、XY位置ごとの Z 補正を与える。2次曲面のフィットには6点以上が必要。
    平面データを与えると2次項が≈0となり平面値を復元する（後方互換）。
    """

    @pytest.fixture
    def plane_points(self):
        # 平面 z = 0.1x + 0.2y（c=0, 2次項なし）を表す非退化な6点。
        # 2次曲面フィットでも2次項≈0となり平面値を復元するはず。
        xys = [
            (0.0, 0.0),
            (10.0, 0.0),
            (0.0, 10.0),
            (10.0, 10.0),
            (5.0, 7.0),
            (3.0, 2.0),
        ]
        return tuple(Point3d(x, y, 0.1 * x + 0.2 * y) for x, y in xys)

    @pytest.fixture
    def height_plane(self, plane_points):
        return HeightPlane(points=plane_points)

    def test_apply_point3d_evaluates_plane(self, height_plane):
        # 平面式 z = 0.1x + 0.2y より (5, 5) では z_offset = 1.5 が加算される
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

        assert result.z == pytest.approx(expected_z_offset, abs=1e-9)

    def test_outside_sample_extent_extrapolates(self, height_plane):
        # 平面 z = 0.1x + 0.2y を全域に外挿。(100, 100) では 0.1*100 + 0.2*100 = 30.0。
        # 平面データなら2次項≈0なので外挿しても平面値が出る。
        result = height_plane.apply(Point3d(100.0, 100.0, 0.0))

        assert result.x == 100.0
        assert result.y == 100.0
        assert result.z == pytest.approx(30.0)

    def test_recovers_convex_paraboloid(self):
        # 既知の凸パラボロイド z = -0.001(x²+y²) + 0.1x + 0.2y + 0.3 を9点でフィットし、
        # サンプルに含まれない評価点で値を復元できることを検証する。
        c, a, b, d, e, f = 0.3, 0.1, 0.2, -0.001, -0.001, 0.0
        grid = [(x, y) for x in (0.0, 5.0, 10.0) for y in (0.0, 5.0, 10.0)]
        points = tuple(
            Point3d(x, y, _quadratic_z(x, y, c, a, b, d, e, f)) for x, y in grid
        )
        hp = HeightPlane(points=points)

        result = hp.apply(Point3d(7.0, 3.0, 0.0))

        assert result.z == pytest.approx(_quadratic_z(7.0, 3.0, c, a, b, d, e, f))

    def test_recovers_twisted_surface(self):
        # ねじれ項 xy を含む面 z = 0.002xy + 0.05x - 0.03y + 1.0 を9点でフィットし、
        # サンプル外の評価点で値を復元できることを検証する。
        c, a, b, d, e, f = 1.0, 0.05, -0.03, 0.0, 0.0, 0.002
        grid = [(x, y) for x in (0.0, 5.0, 10.0) for y in (0.0, 5.0, 10.0)]
        points = tuple(
            Point3d(x, y, _quadratic_z(x, y, c, a, b, d, e, f)) for x, y in grid
        )
        hp = HeightPlane(points=points)

        result = hp.apply(Point3d(7.0, 3.0, 0.0))

        assert result.z == pytest.approx(_quadratic_z(7.0, 3.0, c, a, b, d, e, f))

    def test_apply_point2d_returns_unchanged(self, height_plane):
        point = Point2d(5.0, 5.0)

        result = height_plane.apply(point)

        assert isinstance(result, Point2d)
        assert result == point

    def test_inverse_roundtrip_on_plane(self, height_plane):
        point = Point3d(5.0, 5.0, 100.0)

        transformed = height_plane.apply(point)
        restored = height_plane.inverse().apply(transformed)

        assert restored.x == pytest.approx(point.x)
        assert restored.y == pytest.approx(point.y)
        assert restored.z == pytest.approx(point.z)

    def test_inverse_roundtrip_on_quadratic_surface(self):
        # 2次曲面（反り＋ねじれ）でも inverse のラウンドトリップが成立する。
        c, a, b, d, e, f = 0.3, 0.1, 0.2, -0.001, -0.0005, 0.002
        grid = [(x, y) for x in (0.0, 5.0, 10.0) for y in (0.0, 5.0, 10.0)]
        points = tuple(
            Point3d(x, y, _quadratic_z(x, y, c, a, b, d, e, f)) for x, y in grid
        )
        hp = HeightPlane(points=points)
        point = Point3d(7.0, 3.0, 100.0)

        transformed = hp.apply(point)
        restored = hp.inverse().apply(transformed)

        assert restored.x == pytest.approx(point.x)
        assert restored.y == pytest.approx(point.y)
        assert restored.z == pytest.approx(point.z)

    @pytest.mark.parametrize(
        "points",
        [
            (),
            (Point3d(0.0, 0.0, 0.0),),
            (Point3d(0.0, 0.0, 0.0), Point3d(1.0, 1.0, 1.0)),
            (
                Point3d(0.0, 0.0, 0.0),
                Point3d(1.0, 0.0, 0.0),
                Point3d(0.0, 1.0, 0.0),
                Point3d(1.0, 1.0, 0.0),
                Point3d(2.0, 0.0, 0.0),
            ),
        ],
    )
    def test_fewer_than_six_points_raises_value_error(self, points):
        with pytest.raises(ValueError, match="6点以上"):
            HeightPlane(points=points)

    def test_collinear_points_raises_value_error(self):
        # 共線な6点は design行列 [1,x,y,x²,y²,xy] の rank が6未満となり退化配置として弾かれる。
        points = tuple(Point3d(float(i), float(i), float(i)) for i in range(6))

        with pytest.raises(ValueError, match="退化"):
            HeightPlane(points=points)

    def test_least_squares_fits_noisy_quadratic_points(self):
        # ノイズ付きの2次曲面サンプルから最小二乗で係数を復元できる。
        rng = np.random.default_rng(seed=42)
        c, a, b, d, e, f = 0.3, 0.1, 0.2, -0.001, -0.001, 0.002
        xys = [
            (0.0, 0.0),
            (10.0, 0.0),
            (0.0, 10.0),
            (10.0, 10.0),
            (5.0, 5.0),
            (5.0, 0.0),
            (2.0, 8.0),
            (8.0, 2.0),
            (3.0, 6.0),
        ]
        noise = rng.normal(0.0, 0.005, size=len(xys))
        points = tuple(
            Point3d(x, y, _quadratic_z(x, y, c, a, b, d, e, f) + n)
            for (x, y), n in zip(xys, noise, strict=True)
        )
        hp = HeightPlane(points=points)

        result = hp.apply(Point3d(5.0, 5.0, 0.0))

        assert result.z == pytest.approx(
            _quadratic_z(5.0, 5.0, c, a, b, d, e, f), abs=0.05
        )
