"""重複する銅箔位置合わせ領域の仕様テスト."""

import pytest
import shapely

from pcbasm.geometry import Identity, Point2d, Rotation, Transform
from pcbasm.posctrl import (
    AlignmentRegion,
    CopperProjector,
    plan_alignment_regions,
)

PPM = 10.0
IMAGE_SIZE = (400, 400)
REGION_SIZE_PX = 100
REGION_SIZE_MM = REGION_SIZE_PX / PPM


def _square(cx: float, cy: float, half: float) -> shapely.Polygon:
    return shapely.box(cx - half, cy - half, cx + half, cy + half)


def _projector(
    polygons: list[shapely.Polygon],
    board_transform: Transform = Identity(),
) -> CopperProjector:
    return CopperProjector(
        polygons=polygons,
        board_transform=board_transform,
        offset_transform=Identity(),
        pixel_per_mm=PPM,
        image_size=IMAGE_SIZE,
    )


def _plan(
    pad_centers: list[Point2d],
    *,
    polygons: list[shapely.Polygon] | None = None,
    outline: shapely.Polygon | None = None,
    board_transform: Transform = Identity(),
    region_size_px: int = REGION_SIZE_PX,
    overlap: float = 0.5,
) -> list[AlignmentRegion]:
    return plan_alignment_regions(
        _projector(
            [_square(0.0, 0.0, 25.0)] if polygons is None else polygons,
            board_transform,
        ),
        board_transform,
        pad_centers,
        outline=_square(0.0, 0.0, 30.0) if outline is None else outline,
        region_size_px=region_size_px,
        overlap=overlap,
        image_size=IMAGE_SIZE,
        tour_start=Point2d(0.0, 0.0),
    )


class TestAlignmentRegion:
    """AlignmentRegion.covers は実際の board footprint で所属を判定する."""

    def test_covers_interior_and_boundary_points(self):
        area = _square(2.0, -1.0, 5.0)
        region = AlignmentRegion(
            index=0,
            board_center=Point2d(2.0, -1.0),
            anchor=Point2d(12.0, 9.0),
            roi=(150, 150, 250, 250),
            board_area=area,
        )

        assert region.covers(Point2d(2.0, -1.0))
        assert region.covers(Point2d(7.0, -1.0))
        assert region.covers(Point2d(7.0, 4.0))
        assert not region.covers(Point2d(7.001, -1.0))


class TestPlanAlignmentRegions:
    """Pixel 格子、pad 所属、外周への張り出しをまとめて検証する."""

    def test_half_overlap_uses_half_region_stride(self):
        pads = [Point2d(-7.0, 0.0), Point2d(7.0, 0.0)]

        regions = _plan(pads, overlap=0.5)

        xs = sorted({round(region.board_center.x, 9) for region in regions})
        gaps_px = [
            (right - left) * PPM for left, right in zip(xs, xs[1:], strict=False)
        ]
        assert gaps_px
        assert min(gaps_px) == pytest.approx(REGION_SIZE_PX * 0.5, abs=1e-6)

    def test_only_regions_covering_a_target_pad_are_returned(self):
        pads = [Point2d(-7.0, 0.0), Point2d(7.0, 0.0)]

        regions = _plan(pads)

        assert regions
        assert all(any(region.covers(pad) for pad in pads) for region in regions)

    def test_interior_pad_is_covered_by_multiple_overlapping_regions(self):
        pad = Point2d(0.0, 0.0)

        regions = _plan([pad])

        covering = [region for region in regions if region.covers(pad)]
        assert len(covering) > 1
        assert len({region.index for region in covering}) == len(covering)

    @pytest.mark.parametrize(
        "board_transform",
        [Identity(), Rotation(23.0)],
    )
    def test_edge_pad_is_covered_by_region_crossing_outline(
        self,
        board_transform: Transform,
    ):
        outline = shapely.box(-20.0, -15.0, 20.0, 15.0)
        pad = Point2d(-19.0, -14.0)

        regions = _plan(
            [pad],
            outline=outline,
            board_transform=board_transform,
        )

        assert regions
        assert any(region.covers(pad) for region in regions)
        assert any(not outline.covers(region.board_area) for region in regions)

    def test_grid_starts_one_overlap_width_outside_outline(self):
        outline = shapely.box(-20.0, -15.0, 20.0, 15.0)
        overlap = 0.5

        regions = _plan(
            [Point2d(-19.0, -14.0)],
            outline=outline,
            overlap=overlap,
        )

        margin_mm = REGION_SIZE_MM * overlap
        assert min(region.board_area.bounds[0] for region in regions) == pytest.approx(
            outline.bounds[0] - margin_mm
        )
        assert min(region.board_area.bounds[1] for region in regions) == pytest.approx(
            outline.bounds[1] - margin_mm
        )

    @pytest.mark.parametrize(
        ("pad_centers", "polygons", "outline"),
        [
            ([], [_square(0.0, 0.0, 10.0)], _square(0.0, 0.0, 20.0)),
            (
                [Point2d(0.0, 0.0)],
                [],
                _square(0.0, 0.0, 20.0),
            ),
            (
                [Point2d(0.0, 0.0)],
                [_square(0.0, 0.0, 10.0)],
                shapely.Polygon(),
            ),
        ],
        ids=["no-pads", "no-copper", "empty-outline"],
    )
    def test_empty_inputs_yield_no_regions(self, pad_centers, polygons, outline):
        assert _plan(pad_centers, polygons=polygons, outline=outline) == []


class TestPlanAlignmentRegionsValidation:
    """領域寸法と overlap の公開入力検証."""

    @pytest.mark.parametrize("region_size_px", [0, -1, True, 1.5])
    def test_rejects_non_positive_region_size(self, region_size_px):
        with pytest.raises(ValueError, match="region_size_px"):
            _plan([Point2d(0.0, 0.0)], region_size_px=region_size_px)

    def test_rejects_region_larger_than_image(self):
        with pytest.raises(ValueError, match="region_size_px"):
            _plan([Point2d(0.0, 0.0)], region_size_px=min(IMAGE_SIZE) + 1)

    @pytest.mark.parametrize("overlap", [-0.01, 1.0, 1.01, float("nan")])
    def test_rejects_overlap_outside_half_open_unit_interval(self, overlap):
        with pytest.raises(ValueError, match="overlap"):
            _plan([Point2d(0.0, 0.0)], overlap=overlap)
