"""Paste route planning public-contract tests."""

import pytest
from shapely import Polygon

from pcbasm.geometry import Point2d
from pcbasm.pasting import (
    LevelSetting,
    PasteOverride,
    PasteSettingsModel,
    plan_paste_route,
    routed_enabled_pads,
)
from pcbasm.pcb import Component, Layer, Pad, PadHierarchy, build_pad_hierarchy


def _rect(cx: float, cy: float, w: float, h: float) -> Polygon:
    hw, hh = w / 2.0, h / 2.0
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
    width: float,
    height: float,
) -> Pad:
    return Pad(
        designator=designator,
        pad_number=pad_number,
        net_name="",
        layer=Layer.TOP,
        polygon=_rect(center.x, center.y, width, height),
    )


def _pad_id(pad: Pad) -> str:
    return f"{pad.designator}.{pad.pad_number}"


def _component(designator: str, package: str) -> Component:
    return Component(
        designator=designator,
        value="",
        package=package,
        position=Point2d(x=0.0, y=0.0),
        rotation=0.0,
        layer=Layer.TOP,
    )


def _full_base() -> PasteOverride:
    """全 override 項目が非 None の base override（= L0 確定値）."""
    return PasteOverride(
        dispense_mode="auto",
        paste_height=0.05,
        ul_per_mm2=0.1,
        prime_extra_delay=0.8,
        bead_width_factor=1.0,
        overlap=0.0,
        boundary_margin=0.0,
    )


class TestPlanPasteRoute:
    """plan_paste_route groups by shape and routes within each group."""

    def test_larger_shape_group_routes_before_smaller_group_with_one_based_order(
        self,
    ):
        small_a = _pad("R1", "1", center=Point2d(20.0, 0.0), width=0.5, height=1.0)
        large_a = _pad("U1", "1", center=Point2d(10.0, 0.0), width=2.0, height=2.0)
        small_b = _pad("R1", "2", center=Point2d(21.0, 0.0), width=0.5, height=1.0)
        large_b = _pad("U1", "2", center=Point2d(11.0, 0.0), width=2.0, height=2.0)

        stops = plan_paste_route([small_a, large_a, small_b, large_b])

        assert [stop.order for stop in stops] == [1, 2, 3, 4]
        assert {_pad_id(stop.pad) for stop in stops[:2]} == {"U1.1", "U1.2"}
        assert {_pad_id(stop.pad) for stop in stops[2:]} == {"R1.1", "R1.2"}
        assert stops[0].group_label == stops[1].group_label
        assert stops[2].group_label == stops[3].group_label
        assert stops[0].group_label != stops[2].group_label
        assert [stop.area for stop in stops[:2]] == [
            pytest.approx(large_a.area),
            pytest.approx(large_b.area),
        ]

    def test_same_group_routes_from_start_by_nearest_stop(self):
        far = _pad("U1", "far", center=Point2d(6.0, 0.0), width=1.0, height=1.0)
        near = _pad("U1", "near", center=Point2d(1.0, 0.0), width=1.0, height=1.0)
        mid = _pad("U1", "mid", center=Point2d(3.0, 0.0), width=1.0, height=1.0)

        stops = plan_paste_route(
            [far, near, mid],
            start=Point2d(0.0, 0.0),
        )

        assert [_pad_id(stop.pad) for stop in stops] == [
            "U1.near",
            "U1.mid",
            "U1.far",
        ]
        assert len({stop.group_label for stop in stops}) == 1


class TestRoutedEnabledPads:
    """routed_enabled_pads は有効 pad の絞り込みと順路計算を 1 手で行う。"""

    @staticmethod
    def _fixture() -> tuple[list[Pad], PasteSettingsModel, PadHierarchy]:
        components = [_component("R1", "0402"), _component("U1", "QFN-8")]
        pads = [
            _pad("R1", "1", center=Point2d(20.0, 0.0), width=0.5, height=1.0),
            _pad("R1", "2", center=Point2d(21.0, 0.0), width=0.5, height=1.0),
            _pad("U1", "1", center=Point2d(10.0, 0.0), width=2.0, height=2.0),
            _pad("U1", "2", center=Point2d(11.0, 0.0), width=2.0, height=2.0),
        ]
        hierarchy = build_pad_hierarchy(components, pads)
        return pads, PasteSettingsModel(base=_full_base()), hierarchy

    def test_matches_plan_paste_route_when_all_enabled(self):
        pads, model, hierarchy = self._fixture()

        routed = routed_enabled_pads(pads, hierarchy, model)

        assert routed == [stop.pad for stop in plan_paste_route(pads)]

    def test_disabled_pad_is_excluded_from_route(self):
        pads, model, hierarchy = self._fixture()
        model = PasteSettingsModel(
            base=_full_base(),
            levels={("L4", "U1", "2"): LevelSetting(enabled=False)},
        )

        routed = routed_enabled_pads(pads, hierarchy, model)

        assert [_pad_id(pad) for pad in routed] == ["U1.1", "R1.1", "R1.2"]

    def test_pad_without_component_is_routed_for_backward_compat(self):
        # 階層外 pad（対応 Component 無し）は後方互換で有効扱いのまま順路に乗る。
        pads, model, hierarchy = self._fixture()
        orphan = _pad("X9", "1", center=Point2d(30.0, 0.0), width=0.5, height=1.0)

        routed = routed_enabled_pads([*pads, orphan], hierarchy, model)

        assert orphan in routed
