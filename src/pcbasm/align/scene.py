"""シミュレーションのカメラに映す基板（4 隅の基準円と TOP 銅箔を機械座標の図形にする）."""

from shapely import Point, Polygon

from pcbasm.board import Board, Layer
from pcbasm.config.reference_point import ReferencePointConfig
from pcbasm.geometry import Shift, Transform, transform_polygon

from .board import Corner, corner_offset, marker_position

_CORNERS = (Corner.TOP_LEFT, Corner.TOP_RIGHT, Corner.BOTTOM_RIGHT, Corner.BOTTOM_LEFT)


def placed_on_reference(reference: ReferencePointConfig) -> Shift:
    """左上の基準円が基準点にちょうど重なる置き方（board → 機械座標）."""
    offset = corner_offset(reference.offsets, Corner.TOP_LEFT)
    return Shift(reference.x - offset.x, reference.y - offset.y)


def board_scene(
    board: Board,
    reference: ReferencePointConfig,
    placement: Transform,
    copper: Transform,
) -> list[Polygon]:
    """基板の 4 隅の基準円を placement で、TOP 銅箔を copper で置いた図形を機械座標で返す.

    Args:
        board: 置く基板
        reference: 基準円の径と、基板の角からの位置
        placement: 基準円を置く変換（board → 機械座標）
        copper: 銅箔を置く変換（基準円に対する銅箔のずれを模すときだけ placement と変える）
    """
    markers = (
        placement.apply(marker_position(board.outline, reference.offsets, corner))
        for corner in _CORNERS
    )
    return [
        *(
            Point(marker.x, marker.y).buffer(reference.target_diameter / 2, 32)
            for marker in markers
        ),
        *(
            transform_polygon(island.polygon, copper)
            for island in board.copper
            if island.layer == Layer.TOP
        ),
    ]
