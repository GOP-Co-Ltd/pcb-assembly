"""Initial purge pad resolution public-contract tests."""

import pytest
from shapely import Polygon

from pcbasm.geometry import Point2d
from pcbasm.pasting import ResolvedInitialPurge, resolve_initial_purge
from pcbasm.pcb import Component, Layer, Pad, build_pad_hierarchy


def _rect(cx: float, cy: float, w: float = 1.0, h: float = 1.0) -> Polygon:
    hw = w / 2.0
    hh = h / 2.0
    return Polygon(
        [
            (cx - hw, cy - hh),
            (cx + hw, cy - hh),
            (cx + hw, cy + hh),
            (cx - hw, cy + hh),
        ]
    )


def _pad(
    designator: str,
    pad_number: str,
    *,
    center: Point2d,
    layer: Layer = Layer.TOP,
) -> Pad:
    return Pad(
        designator=designator,
        pad_number=pad_number,
        net_name="",
        layer=layer,
        polygon=_rect(center.x, center.y),
    )


def _hierarchy(pads: list[Pad]):
    components = [
        Component(
            designator=designator,
            value="",
            package="0402",
            position=Point2d(0.0, 0.0),
            rotation=0.0,
            layer=Layer.TOP,
        )
        for designator in sorted({pad.designator for pad in pads})
    ]
    return build_pad_hierarchy(components, pads)


class TestResolveInitialPurge:
    """resolve_initial_purge selects the purge target from routed/top pads."""

    def test_default_uses_first_routed_enabled_pad(self):
        later = _pad("R1", "1", center=Point2d(5.0, 0.0))
        first_routed = _pad("U1", "2", center=Point2d(1.0, 2.0))
        hierarchy = _hierarchy([later, first_routed])

        resolved, error = resolve_initial_purge(
            amount_ul=0.1,
            pad_id=None,
            hierarchy=hierarchy,
            routed_pads=[first_routed, later],
        )

        assert error is None
        assert isinstance(resolved, ResolvedInitialPurge)
        assert resolved.pad_id == "U1.2"
        assert resolved.pad.center == first_routed.center
        assert resolved.amount_ul == pytest.approx(0.1)

    def test_explicit_disabled_top_pad_is_allowed(self):
        enabled = _pad("R1", "1", center=Point2d(0.0, 0.0))
        disabled = _pad("U1", "1", center=Point2d(3.0, 4.0))
        hierarchy = _hierarchy([enabled, disabled])

        resolved, error = resolve_initial_purge(
            amount_ul=0.25,
            pad_id="U1.1",
            hierarchy=hierarchy,
            routed_pads=[enabled],
        )

        assert error is None
        assert isinstance(resolved, ResolvedInitialPurge)
        assert resolved.pad_id == "U1.1"
        assert resolved.pad.center == disabled.center
        assert resolved.amount_ul == pytest.approx(0.25)

    def test_amount_zero_disables_initial_purge(self):
        pad = _pad("R1", "1", center=Point2d(0.0, 0.0))
        hierarchy = _hierarchy([pad])

        resolved, error = resolve_initial_purge(
            amount_ul=0.0,
            pad_id=None,
            hierarchy=hierarchy,
            routed_pads=[pad],
        )

        assert error is None
        assert resolved is None

    def test_unknown_explicit_pad_returns_error_text(self):
        pad = _pad("R1", "1", center=Point2d(0.0, 0.0))
        hierarchy = _hierarchy([pad])

        resolved, error = resolve_initial_purge(
            amount_ul=0.1,
            pad_id="R9.9",
            hierarchy=hierarchy,
            routed_pads=[pad],
        )

        assert resolved is None
        assert error is not None
        assert "R9.9" in error
        assert "未知" in error

    def test_bottom_explicit_pad_returns_error_text(self):
        top = _pad("R1", "1", center=Point2d(0.0, 0.0))
        bottom = _pad("R3", "1", center=Point2d(1.0, 1.0), layer=Layer.BOTTOM)
        hierarchy = _hierarchy([top, bottom])

        resolved, error = resolve_initial_purge(
            amount_ul=0.1,
            pad_id="R3.1",
            hierarchy=hierarchy,
            routed_pads=[top],
        )

        assert resolved is None
        assert error is not None
        assert "R3.1" in error
        assert "Top" in error
