import pytest

from pcbasm.geometry import Point3d, sort_by_nearest


class TestSortByNearest:
    """sort_by_nearest関数のテスト."""

    @pytest.mark.parametrize(
        ("positions", "start", "expected"),
        [
            ([], Point3d(0.0, 0.0, 0.0), []),
            (
                [Point3d(1.0, 1.0, 0.0)],
                Point3d(0.0, 0.0, 0.0),
                [Point3d(1.0, 1.0, 0.0)],
            ),
            (
                [
                    Point3d(1.0, 0.0, 0.0),
                    Point3d(2.0, 0.0, 0.0),
                    Point3d(3.0, 0.0, 0.0),
                ],
                Point3d(0.0, 0.0, 0.0),
                [
                    Point3d(1.0, 0.0, 0.0),
                    Point3d(2.0, 0.0, 0.0),
                    Point3d(3.0, 0.0, 0.0),
                ],
            ),
            (
                [
                    Point3d(3.0, 0.0, 0.0),
                    Point3d(2.0, 0.0, 0.0),
                    Point3d(1.0, 0.0, 0.0),
                ],
                Point3d(0.0, 0.0, 0.0),
                [
                    Point3d(1.0, 0.0, 0.0),
                    Point3d(2.0, 0.0, 0.0),
                    Point3d(3.0, 0.0, 0.0),
                ],
            ),
        ],
    )
    def test_sort_by_nearest(self, positions, start, expected):
        result = sort_by_nearest(positions, start)

        assert result == expected

    def test_2opt_improves_crossing_path(self):
        """2-optが交差経路を解消することを検証する.

        start=(0,0) から正方形の4頂点を巡回。 NNだと (0,0)→(1,0)→(1,3)→(0,3)→(0,1)
        で交差が発生するが、 2-optにより交差が解消され総距離が短くなる。
        """
        start = Point3d(0.0, 0.0, 0.0)
        positions = [
            Point3d(0.0, 1.0, 0.0),
            Point3d(1.0, 0.0, 0.0),
            Point3d(0.0, 3.0, 0.0),
            Point3d(1.0, 3.0, 0.0),
        ]

        result = sort_by_nearest(positions, start)

        # 全点が含まれている
        assert set(map(tuple, [(p.x, p.y) for p in result])) == {
            (0.0, 1.0),
            (1.0, 0.0),
            (0.0, 3.0),
            (1.0, 3.0),
        }
        # 総距離を計算
        total = (result[0] - start).norm()
        for i in range(len(result) - 1):
            total += (result[i + 1] - result[i]).norm()
        # NN単体の経路長 (0,0)→(1,0)→(1,3)→(0,3)→(0,1): 1+3+1+2=7
        assert total < 7.0
