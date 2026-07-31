"""重複するpixel ROIから銅箔照合領域を計画する."""

from collections.abc import Sequence

import attrs
import numpy as np
from shapely import Point, Polygon

from pcbasm.geometry import Point2d, Transform, sort_by_nearest
from pcbasm.posctrl.copper import CopperProjector, PixelRect, centered_roi


@attrs.frozen
class AlignmentRegion:
    """1つの銅箔照合領域."""

    index: int
    board_center: Point2d
    anchor: Point2d
    roi: PixelRect
    board_area: Polygon

    def covers(self, board_point: Point2d) -> bool:
        """領域が board 座標の点を境界込みで覆うか返す."""
        return self.board_area.covers(Point(board_point.x, board_point.y))


def plan_alignment_regions(
    projector: CopperProjector,
    board_transform: Transform,
    pad_centers: Sequence[Point2d],
    *,
    outline: Polygon,
    region_size_px: int,
    overlap: float,
    image_size: tuple[int, int],
    tour_start: Point2d,
) -> list[AlignmentRegion]:
    """塗布対象pad中心を覆う重複ROIを計画し、巡回順に返す."""
    if (
        isinstance(region_size_px, bool)
        or not isinstance(region_size_px, int)
        or region_size_px < 1
    ):
        raise ValueError(f"region_size_pxは1以上である必要があります: {region_size_px}")
    if region_size_px > min(image_size):
        raise ValueError(
            f"region_size_px {region_size_px} が画像サイズ {image_size} を超えています"
        )
    if (
        isinstance(overlap, bool)
        or not isinstance(overlap, (int, float))
        or not np.isfinite(overlap)
        or not 0.0 <= overlap < 1.0
    ):
        raise ValueError(f"overlapは0以上1未満である必要があります: {overlap}")
    if not projector.polygons or not pad_centers or outline.is_empty:
        return []

    minx, miny, maxx, maxy = outline.bounds
    reference_center = Point2d((minx + maxx) / 2, (miny + maxy) / 2)
    matrix, shift = projector.board_to_pixel_affine(
        board_transform.apply(reference_center)
    )
    inverse = np.linalg.inv(matrix)
    outline_pixels = np.asarray(outline.exterior.coords) @ matrix.T + shift

    half = region_size_px / 2
    stride = region_size_px * (1.0 - overlap)
    first_start = outline_pixels.min(axis=0) - region_size_px * overlap
    region_counts = np.ceil((outline_pixels.max(axis=0) - first_start) / stride).astype(
        int
    )
    corner_offsets_px = np.array(
        [[-half, -half], [half, -half], [half, half], [-half, half]]
    )

    candidates: list[tuple[Point2d, Polygon]] = []
    for column in range(int(region_counts[0])):
        for row in range(int(region_counts[1])):
            center_px = (
                first_start + np.array([column, row]) * stride + np.array([half, half])
            )
            board_center_xy = (center_px - shift) @ inverse.T
            board_center = Point2d(
                x=float(board_center_xy[0]), y=float(board_center_xy[1])
            )
            corners = (center_px + corner_offsets_px - shift) @ inverse.T
            board_area = Polygon(corners)
            if not any(
                board_area.covers(Point(center.x, center.y)) for center in pad_centers
            ):
                continue
            candidates.append((board_center, board_area))

    roi = centered_roi(image_size, region_size_px)
    ordered = sort_by_nearest(
        [
            (board_transform.apply(board_center), board_center, board_area)
            for board_center, board_area in candidates
        ],
        tour_start.to3d(),
        key=lambda entry: entry[0].to3d(),
    )
    return [
        AlignmentRegion(
            index=index,
            board_center=board_center,
            anchor=anchor,
            roi=roi,
            board_area=board_area,
        )
        for index, (anchor, board_center, board_area) in enumerate(ordered)
    ]
