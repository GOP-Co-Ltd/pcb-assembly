"""塗布向け位置合わせ合成（refinement_targets）の公開契約."""

import shapely

from pcbasm.pasting.alignment import refinement_targets
from pcbasm.pcb import Layer, Pad


def _pad(designator: str, width: float, height: float) -> Pad:
    return Pad(
        designator=designator,
        pad_number="1",
        net_name="NET",
        layer=Layer.TOP,
        polygon=shapely.box(0.0, 0.0, width, height),
    )


class TestRefinementTargets:
    def test_keeps_only_pads_with_short_side_within_limit(self):
        narrow = _pad("R1", 0.3, 1.2)
        rotated_narrow = _pad("R2", 1.2, 0.3)
        wide = _pad("U1", 2.0, 2.0)

        targets = refinement_targets(
            [wide, narrow, rotated_narrow], max_short_side_mm=0.5
        )

        assert targets == (narrow, rotated_narrow)

    def test_non_positive_limit_selects_nothing(self):
        assert refinement_targets([_pad("R1", 0.3, 1.2)], max_short_side_mm=0.0) == ()

    def test_empty_polygon_is_skipped(self):
        empty = Pad(
            designator="X1",
            pad_number="1",
            net_name="",
            layer=Layer.TOP,
            polygon=shapely.Polygon(),
        )

        assert refinement_targets([empty], max_short_side_mm=1.0) == ()
