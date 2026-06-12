from typing import NamedTuple

import pytest

from pcbasm.geometry import Point3d, sort_by_nearest


class _Stop(NamedTuple):
    """Key 形式の検証用: 位置を属性に持つ任意オブジェクト."""

    name: str
    position: Point3d


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

    def test_key_sorts_objects_by_extracted_positions(self):
        """Key で Point3d を抽出し、オブジェクト列を巡回順に並べ替える."""
        stops = [
            _Stop("far", Point3d(3.0, 0.0, 0.0)),
            _Stop("near", Point3d(1.0, 0.0, 0.0)),
            _Stop("mid", Point3d(2.0, 0.0, 0.0)),
        ]

        result = sort_by_nearest(
            stops, Point3d(0.0, 0.0, 0.0), key=lambda s: s.position
        )

        assert [s.name for s in result] == ["near", "mid", "far"]

    def test_key_preserves_elements_with_identical_positions(self):
        """同一座標の要素が両方とも結果に残る（dict 逆引きで潰れない）."""
        stops = [
            _Stop("a", Point3d(1.0, 0.0, 0.0)),
            _Stop("b", Point3d(1.0, 0.0, 0.0)),
            _Stop("c", Point3d(2.0, 0.0, 0.0)),
        ]

        result = sort_by_nearest(
            stops, Point3d(0.0, 0.0, 0.0), key=lambda s: s.position
        )

        assert len(result) == 3
        assert {s.name for s in result} == {"a", "b", "c"}
        # 同一座標の2要素は隣接し、より遠い c が最後
        assert {s.name for s in result[:2]} == {"a", "b"}
        assert result[2].name == "c"
