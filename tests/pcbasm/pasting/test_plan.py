"""塗布の前計画（plan_paste）の公開契約.

pad 編集画面の preview と塗布ジョブが同じ計画を使うので、ここで固定する値はどちらにも効く。
"""

import attrs
import pytest

from pcbasm.board import Board, Layer
from pcbasm.config import load_machine
from pcbasm.config.solder import VolumeCorrectionConfig
from pcbasm.geometry import Point2d
from pcbasm.kicad import load_board
from pcbasm.pasting.hierarchy import PadHierarchy
from pcbasm.pasting.overrides import BoardSession, PasteSettingsModel
from pcbasm.pasting.params import PasteParams
from pcbasm.pasting.plan import PastePlan, plan_paste
from tests.support.paths import LED_BLINKER_PCB, TEST_CONFIG_DIR

_BASE = PasteParams(
    dispense_mode="auto",
    line_direction="unconstrained",
    paste_height=0.05,
    ul_per_mm2=0.1,
    prime_extra_delay=0.8,
    bead_width_factor=1.0,
    overlap=0.0,
    boundary_margin=0.0,
    micro_line_z_start=0.07,
    micro_line_z_end=0.07,
)
_DISABLED = VolumeCorrectionConfig()
_ENABLED = VolumeCorrectionConfig(calibration_file="cal.paste-volume.json")


@pytest.fixture(scope="module")
def pcb() -> Board:
    return load_board(LED_BLINKER_PCB)


@pytest.fixture(scope="module")
def hierarchy(pcb: Board) -> PadHierarchy:
    return PadHierarchy.build(pcb.components, pcb.pads)


def _model() -> PasteSettingsModel:
    return PasteSettingsModel(base=_BASE)


def _plan(
    pcb: Board,
    hierarchy: PadHierarchy,
    model: PasteSettingsModel,
    *,
    initial_purge_ul: float = 0.5,
    volume_correction: VolumeCorrectionConfig = _DISABLED,
) -> PastePlan:
    config = attrs.evolve(
        load_machine(TEST_CONFIG_DIR / "machine.toml").require_solder(),
        initial_purge_ul=initial_purge_ul,
        volume_correction=volume_correction,
    )
    return plan_paste(
        BoardSession(
            source_pcb="led_blinker.kicad_pcb",
            base_config=config,
            board=pcb,
            hierarchy=hierarchy,
            board_signature=hierarchy.signature(),
            model=model,
        )
    )


def _pad_ids(pads) -> list[str]:
    return [pad.ref for pad in pads]


class TestPlanPaste:
    """塗布の対象・順路・pad ごとの設定・初回パージ."""

    def test_routes_every_enabled_top_pad(self, pcb, hierarchy):
        plan = _plan(pcb, hierarchy, _model())

        assert plan.error is None
        top_ids = {p.ref for p in pcb.pads if p.layer is Layer.TOP}
        assert set(_pad_ids(plan.routed_pads)) == top_ids
        assert len(plan.pads) == len(top_ids)
        assert plan.disabled_count == 0
        assert [stop.order for stop in plan.route] == list(range(1, len(top_ids) + 1))

    def test_default_initial_purge_is_the_largest_pad_center(self, pcb, hierarchy):
        plan = _plan(pcb, hierarchy, _model())
        purge = plan.initial_purge

        assert purge.resolved is not None
        largest = max(plan.routed_pads, key=lambda pad: pad.area)
        assert purge.resolved.point == largest.center
        assert purge.default_point == largest.center
        assert purge.resolved.source == "default"
        assert purge.resolved.amount_ul == 0.5

    def test_zero_purge_amount_disables_initial_purge(self, pcb, hierarchy):
        plan = _plan(pcb, hierarchy, _model(), initial_purge_ul=0.0)

        assert plan.error is None
        assert plan.initial_purge.resolved is None

    def test_disabled_pads_leave_route_but_count_as_disabled(self, pcb, hierarchy):
        keys, _ = hierarchy.l4_keys_for_pad_ids(["U1.1", "U1.2"])
        model = _model().with_pads_enabled(keys, enabled=False)

        plan = _plan(pcb, hierarchy, model)

        routed_ids = _pad_ids(plan.routed_pads)
        assert "U1.1" not in routed_ids
        assert "U1.2" not in routed_ids
        assert plan.disabled_count == 2

    def test_explicit_purge_point_overrides_the_route_head(self, pcb, hierarchy):
        point = Point2d(pcb.outline.width / 2.0, pcb.outline.height / 2.0)

        plan = _plan(pcb, hierarchy, _model().with_initial_purge_point(point))

        assert plan.error is None
        assert plan.initial_purge.resolved is not None
        assert plan.initial_purge.resolved.point == point
        assert plan.initial_purge.resolved.source == "explicit"

    def test_purge_point_outside_the_outline_is_reported_with_the_plan(
        self, pcb, hierarchy
    ):
        """Pad 編集画面は理由を見せて直させるので、計画そのものは返す."""
        model = _model().with_initial_purge_point(Point2d(-50.0, -50.0))

        plan = _plan(pcb, hierarchy, model)

        assert plan.error is not None
        assert "基板外形" in plan.error
        assert plan.routed_pads

    def test_params_for_reflects_level_override(self, pcb, hierarchy):
        (key,), _ = hierarchy.l4_keys_for_pad_ids(["U1.1"])
        model = _model().with_level_patch(key, values={"ul_per_mm2": 0.25})

        plan = _plan(pcb, hierarchy, model, initial_purge_ul=0.0)

        by_id = {p.ref: p for p in plan.routed_pads}
        assert plan.params_for(by_id["U1.1"]).ul_per_mm2 == 0.25
        assert plan.params_for(by_id["U1.2"]) == _BASE

    def test_line_reference_is_the_component_position(self, pcb, hierarchy):
        plan = _plan(pcb, hierarchy, _model())
        positions = {c.designator: c.position for c in pcb.components}

        for pad in plan.routed_pads:
            assert plan.line_reference(pad) == positions.get(pad.designator)

    def test_fill_plan_uses_the_resolved_mode(self, pcb, hierarchy):
        (key,), _ = hierarchy.l4_keys_for_pad_ids(["U1.1"])
        model = _model().with_level_patch(key, values={"dispense_mode": "dot"})

        plan = _plan(pcb, hierarchy, model)

        (pad,) = (p for p in plan.routed_pads if p.ref == "U1.1")
        assert plan.fill_plan(pad).dispense_mode == "dot"


class TestPlanPasteFlowCalibration:
    """運転時補正の解決（設定と基板座標の両方が要る）."""

    def test_is_none_when_the_machine_setting_is_disabled(self, pcb, hierarchy):
        model = _model().with_flow_calibration_points([Point2d(10.0, 10.0)])

        plan = _plan(pcb, hierarchy, model)

        assert plan.error is None
        assert plan.flow_calibration is None

    def test_is_none_when_no_board_point_is_set(self, pcb, hierarchy):
        plan = _plan(pcb, hierarchy, _model(), volume_correction=_ENABLED)

        assert plan.error is None
        assert plan.flow_calibration is None

    def test_plans_the_points_when_both_are_configured(self, pcb, hierarchy):
        points = [Point2d(10.0, 10.0), Point2d(13.0, 10.0)]
        model = _model().with_flow_calibration_points(points)

        plan = _plan(pcb, hierarchy, model, volume_correction=_ENABLED)

        assert plan.error is None
        assert plan.flow_calibration is not None
        assert plan.flow_calibration.points == tuple(points)

    def test_overlapping_crops_are_an_error(self, pcb, hierarchy):
        model = _model().with_flow_calibration_points(
            [Point2d(10.0, 10.0), Point2d(10.5, 10.0)]
        )

        plan = _plan(pcb, hierarchy, model, volume_correction=_ENABLED)

        assert plan.flow_calibration is None
        assert plan.error is not None
        assert "撮影範囲" in plan.error

    def test_a_point_outside_the_outline_is_an_error(self, pcb, hierarchy):
        model = _model().with_flow_calibration_points([Point2d(-50.0, -50.0)])

        plan = _plan(pcb, hierarchy, model, volume_correction=_ENABLED)

        assert plan.flow_calibration is None
        assert plan.error is not None
        assert "基板外形" in plan.error

    def test_an_initial_purge_error_is_reported_first(self, pcb, hierarchy):
        model = (
            _model()
            .with_initial_purge_point(Point2d(-50.0, -50.0))
            .with_flow_calibration_points([Point2d(-50.0, -50.0)])
        )

        plan = _plan(pcb, hierarchy, model, volume_correction=_ENABLED)

        assert plan.error == plan.initial_purge.error
