"""基板共通位置補正を塗布対象へ適用する公開ロジックの仕様テスト."""

import pytest
from shapely import Polygon

from pcbasm.geometry import Compose, Point2d, Rotation, Shift
from pcbasm.pasting import ResolvedInitialPurge, correct_paste_targets
from pcbasm.pcb import Layer, Pad


def _square(cx: float, cy: float, half: float = 0.5) -> Polygon:
    return Polygon(
        [
            (cx - half, cy - half),
            (cx + half, cy - half),
            (cx + half, cy + half),
            (cx - half, cy + half),
        ]
    )


def _pad(designator: str, pad_number: str, x: float, y: float) -> Pad:
    return Pad(
        designator=designator,
        pad_number=pad_number,
        net_name="NET",
        layer=Layer.TOP,
        polygon=_square(x, y),
    )


class TestCorrectPasteTargets:
    """全routed padと初回パージ点へ同じboard空間補正を適用する."""

    def test_corrects_every_routed_polygon_and_explicit_purge_pad(self):
        routed = [
            _pad("R1", "1", 0.0, 0.0),
            _pad("U1", "3", 5.0, 2.0),
        ]
        disabled_purge_pad = _pad("C9", "2", 10.0, -1.0)
        initial_purge = ResolvedInitialPurge(
            amount_ul=0.1,
            pad=disabled_purge_pad,
            pad_id="C9.2",
            source="explicit",
        )
        correction = Compose([Rotation(10.0), Shift(0.3, -0.2)])

        corrected = correct_paste_targets(routed, initial_purge, correction)

        assert len(corrected.polygons) == len(routed)
        for pad, polygon in zip(routed, corrected.polygons, strict=True):
            expected = correction.apply(pad.center)
            assert polygon.centroid.x == pytest.approx(expected.x)
            assert polygon.centroid.y == pytest.approx(expected.y)
        expected_purge = correction.apply(disabled_purge_pad.center)
        assert corrected.initial_purge_point is not None
        assert corrected.initial_purge_point.x == pytest.approx(expected_purge.x)
        assert corrected.initial_purge_point.y == pytest.approx(expected_purge.y)

    def test_without_initial_purge_keeps_purge_point_none(self):
        corrected = correct_paste_targets(
            [_pad("R1", "1", 0.0, 0.0)],
            None,
            Shift(0.2, 0.1),
        )

        assert corrected.initial_purge_point is None
        assert len(corrected.polygons) == 1
