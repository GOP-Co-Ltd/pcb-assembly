"""塗布ジョブ前計画（plan_paste_targets）の公開契約."""

import pytest
import shapely

from pcbasm.pasting.params import PasteParams
from pcbasm.pasting.settings import PasteSettingsModel
from pcbasm.pasting.workflow import plan_paste_targets
from pcbasm.pcb import Layer, Pad, PadHierarchy, PcbFile
from tests.helpers import TESTING_DATA_DIR

_LED_BLINKER = TESTING_DATA_DIR / "led_blinker" / "led_blinker.kicad_pcb"
_BASE = PasteParams(
    dispense_mode="auto",
    line_direction="unconstrained",
    paste_height=0.05,
    ul_per_mm2=0.1,
    prime_extra_delay=0.8,
    bead_width_factor=1.0,
    overlap=0.0,
    boundary_margin=0.0,
)


@pytest.fixture(scope="module")
def pcb() -> PcbFile:
    return PcbFile(_LED_BLINKER)


@pytest.fixture(scope="module")
def hierarchy(pcb: PcbFile) -> PadHierarchy:
    return PadHierarchy.build(pcb.components, pcb.pads)


def _model() -> PasteSettingsModel:
    return PasteSettingsModel(base=_BASE)


def _pad_ids(hierarchy: PadHierarchy, pads) -> list[str | None]:
    return [hierarchy.find_pad_id(pad) for pad in pads]


class TestPlanPasteTargets:
    """通常塗布の対象・順路・初回パージ解決."""

    def test_routes_every_enabled_top_pad(self, pcb, hierarchy):
        targets, error = plan_paste_targets(
            pcb, hierarchy, _model(), initial_purge_ul=0.5
        )

        assert error is None
        assert targets is not None
        top_ids = {hierarchy.find_pad_id(p) for p in pcb.pads if p.layer is Layer.TOP}
        assert set(_pad_ids(hierarchy, targets.routed_pads)) == top_ids
        assert len(targets.top_pads) == len(top_ids)
        assert targets.disabled_count == 0
        assert all(p.layer is Layer.TOP for p in targets.routed_pads)

    def test_default_initial_purge_is_route_head_and_not_duplicated(
        self, pcb, hierarchy
    ):
        targets, _ = plan_paste_targets(pcb, hierarchy, _model(), initial_purge_ul=0.5)

        assert targets is not None
        assert targets.initial_purge is not None
        assert targets.initial_purge.pad is targets.routed_pads[0]
        assert targets.initial_purge.amount_ul == 0.5
        assert targets.alignment_pads == targets.routed_pads

    def test_zero_purge_amount_disables_initial_purge(self, pcb, hierarchy):
        targets, error = plan_paste_targets(
            pcb, hierarchy, _model(), initial_purge_ul=0.0
        )

        assert error is None
        assert targets is not None
        assert targets.initial_purge is None
        assert targets.alignment_pads == targets.routed_pads

    def test_disabled_pads_leave_route_but_count_as_disabled(self, pcb, hierarchy):
        model = _model().with_pads_enabled(
            [hierarchy.l4_key_for_pad_id("U1.1"), hierarchy.l4_key_for_pad_id("U1.2")],
            enabled=False,
        )

        targets, _ = plan_paste_targets(pcb, hierarchy, model, initial_purge_ul=0.5)

        assert targets is not None
        routed_ids = _pad_ids(hierarchy, targets.routed_pads)
        assert "U1.1" not in routed_ids
        assert "U1.2" not in routed_ids
        assert targets.disabled_count == 2

    def test_explicit_disabled_purge_pad_is_appended_to_alignment_pads(
        self, pcb, hierarchy
    ):
        model = (
            _model()
            .with_pads_enabled([hierarchy.l4_key_for_pad_id("U1.1")], enabled=False)
            .with_initial_purge_pad_id("U1.1")
        )

        targets, error = plan_paste_targets(pcb, hierarchy, model, initial_purge_ul=0.5)

        assert error is None
        assert targets is not None
        assert targets.initial_purge is not None
        assert hierarchy.find_pad_id(targets.initial_purge.pad) == "U1.1"
        assert targets.alignment_pads[:-1] == targets.routed_pads
        assert targets.alignment_pads[-1] is targets.initial_purge.pad

    def test_unknown_purge_pad_returns_error(self, pcb, hierarchy):
        model = _model().with_initial_purge_pad_id("ZZ9.1")

        targets, error = plan_paste_targets(pcb, hierarchy, model, initial_purge_ul=0.5)

        assert targets is None
        assert error is not None
        assert "ZZ9.1" in error

    def test_params_for_reflects_level_override(self, pcb, hierarchy):
        model = _model().with_level_patch(
            hierarchy.l4_key_for_pad_id("U1.1"), values={"ul_per_mm2": 0.25}
        )
        targets, _ = plan_paste_targets(pcb, hierarchy, model, initial_purge_ul=0.0)

        assert targets is not None
        by_id = {hierarchy.find_pad_id(p): p for p in targets.routed_pads}
        overridden = targets.params_for(by_id["U1.1"])
        inherited = targets.params_for(by_id["U1.2"])
        assert overridden is not None and inherited is not None
        assert overridden.ul_per_mm2 == 0.25
        assert inherited == _BASE

    def test_params_for_pad_outside_hierarchy_is_none(self, pcb, hierarchy):
        targets, _ = plan_paste_targets(pcb, hierarchy, _model(), initial_purge_ul=0.0)
        stray = Pad(
            designator="ZZ9",
            pad_number="1",
            net_name="",
            layer=Layer.TOP,
            polygon=shapely.box(0.0, 0.0, 1.0, 1.0),
        )

        assert targets is not None
        assert targets.params_for(stray) is None
