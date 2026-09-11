import pytest

from pcbasm.geometry import Compose, Path, Point3d, Scale, Shift


class TestPath:
    """Pathクラスのテスト.

    Pathは順序付き3D点列を表す純粋な幾何オブジェクト（速度・タイミングを持たない）。
    """

    @pytest.mark.parametrize(
        ("points", "expected_length"),
        [
            # 空のPathは長さ0
            ([], 0.0),
            # 単一点のPathは長さ0
            ([Point3d(1.0, 2.0, 3.0)], 0.0),
            # 2点 3-4-5: (0,0,0)→(3,4,0) = 5
            ([Point3d(0.0, 0.0, 0.0), Point3d(3.0, 4.0, 0.0)], 5.0),
            # 多区間: (0,0,0)→(3,0,0)→(3,4,0) = 3 + 4 = 7
            (
                [
                    Point3d(0.0, 0.0, 0.0),
                    Point3d(3.0, 0.0, 0.0),
                    Point3d(3.0, 4.0, 0.0),
                ],
                7.0,
            ),
        ],
    )
    def test_length(self, points, expected_length):
        path = Path(points)

        assert path.length() == expected_length

    @pytest.mark.parametrize("points", [[], [Point3d(1.0, 2.0, 3.0)]])
    def test_length_of_a_degenerate_path_is_a_float(self, points):
        # 点塗布は 1 点経路になる。暗黙変換を拒否する metadata schema が
        # int の 0 を受け付けないので、区間が無くても float を返す。
        assert isinstance(Path(points).length(), float)

    def test_transformed_applies_the_transform_to_every_point(self):
        # Composeは self[0] → self[1] の順で適用される。
        # (1,1,1) → Shift(1,2,3) → (2,3,4) → Scale(2,2,2) → (4,6,8)
        path = Path([Point3d(1.0, 1.0, 1.0)])

        result = path.transformed(Compose([Shift(1.0, 2.0, 3.0), Scale(2.0, 2.0, 2.0)]))

        assert result.points == (Point3d(4.0, 6.0, 8.0),)

    def test_transformed_does_not_mutate_original(self):
        original_points = (Point3d(0.0, 0.0, 0.0), Point3d(1.0, 1.0, 1.0))
        path = Path(original_points)

        path.transformed(Shift(10.0, 10.0, 10.0))

        # 元のPathは変換の影響を受けない（イミュータブルな値）
        assert path.points == original_points

    def test_construction_from_list_and_generator_are_equal(self):
        points = [Point3d(0.0, 0.0, 0.0), Point3d(1.0, 2.0, 3.0)]

        from_list = Path(points)
        from_generator = Path(p for p in points)

        assert from_list == from_generator
        assert from_list.points == from_generator.points

    def test_len(self):
        path = Path([Point3d(0.0, 0.0, 0.0), Point3d(1.0, 2.0, 3.0)])

        assert len(path) == 2

    def test_iteration_and_indexing_follow_point_order(self):
        points = [
            Point3d(0.0, 0.0, 0.0),
            Point3d(1.0, 0.0, 0.0),
            Point3d(1.0, 1.0, 0.0),
        ]
        path = Path(points)

        assert list(path) == points
        assert path[0] == points[0]
        assert path[-1] == points[-1]
