"""pcbasm.geometry.polyline のテスト."""

import pytest

from pcbasm.geometry import Point2d, polyline_length, ring_segment


class TestPolylineLength:
    @pytest.mark.parametrize(
        ("points", "expected"),
        [
            ([], 0.0),
            ([Point2d(0.0, 0.0)], 0.0),
            ([Point2d(0.0, 0.0), Point2d(3.0, 4.0)], 5.0),
            ([Point2d(0.0, 0.0), Point2d(1.0, 0.0), Point2d(1.0, 2.0)], 3.0),
        ],
    )
    def test_sums_segment_lengths(self, points: list[Point2d], expected: float):
        assert polyline_length(points) == pytest.approx(expected)


class TestRingSegment:
    _square = [
        Point2d(0.0, 0.0),
        Point2d(1.0, 0.0),
        Point2d(1.0, 1.0),
        Point2d(0.0, 1.0),
    ]

    def test_forward_walk_includes_both_ends(self):
        assert ring_segment(self._square, 0, 2, step=1) == self._square[0:3]

    def test_backward_walk_wraps_around(self):
        assert ring_segment(self._square, 0, 2, step=-1) == [
            self._square[0],
            self._square[3],
            self._square[2],
        ]

    def test_same_index_returns_single_point(self):
        assert ring_segment(self._square, 1, 1, step=1) == [self._square[1]]
