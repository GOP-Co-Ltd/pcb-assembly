"""塗布対象へ基板共通位置補正を適用する純粋ロジック."""

from collections.abc import Sequence

import attrs
from shapely import Polygon

from pcbasm.geometry import Point2d, Transform, transform_polygon
from pcbasm.pasting.initial_purge import ResolvedInitialPurge
from pcbasm.pcb import Pad


@attrs.frozen
class CorrectedPasteTargets:
    """共通補正を適用した塗布polygon列と初回パージ点."""

    polygons: tuple[Polygon, ...]
    initial_purge_point: Point2d | None


def correct_paste_targets(
    routed_pads: Sequence[Pad],
    initial_purge: ResolvedInitialPurge | None,
    board_correction: Transform,
) -> CorrectedPasteTargets:
    """同一のboard空間補正を全routed padと初回パージ点へ適用する."""
    return CorrectedPasteTargets(
        polygons=tuple(
            transform_polygon(pad.polygon, board_correction) for pad in routed_pads
        ),
        initial_purge_point=(
            board_correction.apply(initial_purge.pad.center)
            if initial_purge is not None
            else None
        ),
    )
