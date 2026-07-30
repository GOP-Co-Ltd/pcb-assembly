"""重複する成功領域から pad 別補正を導く仕様テスト."""

import pytest
import shapely

from pcbasm.geometry import Point2d
from pcbasm.posctrl import AlignmentRegion, BoardAlignment, EdgeMatch, RegionAlignment
from pcbasm.vision import Offset

PPM = 10.0


def _alignment(
    index: int,
    board_area: shapely.Polygon,
    displacement: Point2d,
) -> RegionAlignment:
    center = board_area.centroid
    return RegionAlignment(
        region=AlignmentRegion(
            index=index,
            board_center=Point2d(center.x, center.y),
            anchor=Point2d(center.x, center.y),
            roi=(0, 0, 100, 100),
            board_area=board_area,
        ),
        match=EdgeMatch(
            offset=Offset(px=Point2d(0.0, 0.0), pixel_per_mm=PPM),
            rms_distance_px=0.0,
        ),
        displacement=displacement,
        increment=Point2d(0.0, 0.0),
        passes=1,
    )


def _shift_at(alignment: BoardAlignment, point: Point2d) -> Point2d:
    correction = alignment.correction_for(point, designator="U1")
    return correction.apply(point) - point


class TestBoardAlignmentCorrectionFor:
    """覆う全成功領域の machine displacement を算術平均する."""

    @pytest.mark.parametrize(
        ("displacements", "expected"),
        [
            (
                [Point2d(0.10, -0.20), Point2d(0.30, 0.10)],
                Point2d(0.20, -0.05),
            ),
            (
                [
                    Point2d(0.10, -0.20),
                    Point2d(0.30, 0.10),
                    Point2d(-0.10, 0.30),
                    Point2d(0.50, -0.20),
                ],
                Point2d(0.20, 0.00),
            ),
        ],
        ids=["two-regions", "four-regions"],
    )
    def test_returns_arithmetic_mean_of_all_covering_regions(
        self, displacements, expected
    ):
        area = shapely.box(-1.0, -1.0, 1.0, 1.0)
        alignment = BoardAlignment(
            results=tuple(
                _alignment(index, area, displacement)
                for index, displacement in enumerate(displacements)
            )
        )

        shift = _shift_at(alignment, Point2d(0.0, 0.0))

        assert shift.x == pytest.approx(expected.x, abs=1e-12)
        assert shift.y == pytest.approx(expected.y, abs=1e-12)

    def test_boundary_point_uses_every_region_that_covers_it(self):
        alignment = BoardAlignment(
            results=(
                _alignment(
                    0,
                    shapely.box(-2.0, -2.0, 0.0, 2.0),
                    Point2d(0.10, -0.10),
                ),
                _alignment(
                    1,
                    shapely.box(0.0, -2.0, 2.0, 2.0),
                    Point2d(0.30, 0.20),
                ),
            )
        )

        shift = _shift_at(alignment, Point2d(0.0, 0.0))

        assert shift.x == pytest.approx(0.20, abs=1e-12)
        assert shift.y == pytest.approx(0.05, abs=1e-12)

    def test_different_pads_select_different_covering_region_sets(self):
        alignment = BoardAlignment(
            results=(
                _alignment(
                    0,
                    shapely.box(-2.0, -2.0, 0.0, 2.0),
                    Point2d(0.10, -0.10),
                ),
                _alignment(
                    1,
                    shapely.box(0.0, -2.0, 2.0, 2.0),
                    Point2d(0.40, 0.20),
                ),
            )
        )

        left = _shift_at(alignment, Point2d(-1.0, 0.0))
        right = _shift_at(alignment, Point2d(1.0, 0.0))

        assert left.x == pytest.approx(0.10, abs=1e-12)
        assert left.y == pytest.approx(-0.10, abs=1e-12)
        assert right.x == pytest.approx(0.40, abs=1e-12)
        assert right.y == pytest.approx(0.20, abs=1e-12)

    def test_returns_a_pure_translation(self):
        alignment = BoardAlignment(
            results=(
                _alignment(
                    0,
                    shapely.box(-2.0, -2.0, 2.0, 2.0),
                    Point2d(0.25, -0.15),
                ),
            )
        )
        correction = alignment.correction_for(Point2d(0.0, 0.0), designator="R1")

        for point in (Point2d(0.0, 0.0), Point2d(100.0, -50.0)):
            shift = correction.apply(point) - point
            assert shift.x == pytest.approx(0.25, abs=1e-12)
            assert shift.y == pytest.approx(-0.15, abs=1e-12)

    def test_no_covering_success_region_raises_without_nearest_fallback(self):
        alignment = BoardAlignment(
            results=(
                _alignment(
                    0,
                    shapely.box(-1.0, -1.0, 1.0, 1.0),
                    Point2d(0.25, -0.15),
                ),
            )
        )

        with pytest.raises(ValueError) as exc_info:
            alignment.correction_for(Point2d(20.0, 20.0), designator="C17")

        assert "C17" in str(exc_info.value)
