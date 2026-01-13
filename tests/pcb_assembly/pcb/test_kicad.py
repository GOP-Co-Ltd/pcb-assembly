import pytest

from pcb_assembly.pcb.kicad import extract_components, extract_pads
from pcb_assembly.pcb.utils import Layer
from tests.helpers import TESTING_DATA_DIR

LED_BLINKER_PCB = TESTING_DATA_DIR / "led_blinker" / "led_blinker.kicad_pcb"


class TestExtractComponents:
    """extract_components関数のテスト."""

    def test_extracts_all_components(self):
        components = extract_components(LED_BLINKER_PCB)

        assert len(components) == 7

    def test_extracts_component_attributes(self):
        components = extract_components(LED_BLINKER_PCB)

        u1 = next(c for c in components if c.designator == "U1")
        assert u1.value == "ATtiny85"
        assert u1.package == "SOT-23-6"
        assert u1.x == pytest.approx(105.0)
        assert u1.y == pytest.approx(100.0)
        assert u1.rotation == pytest.approx(0.0)
        assert u1.layer == Layer.TOP

    def test_extracts_rotated_component(self):
        components = extract_components(LED_BLINKER_PCB)

        d1 = next(c for c in components if c.designator == "D1")
        assert d1.rotation == pytest.approx(90.0)

    def test_extracts_bottom_layer_component(self):
        components = extract_components(LED_BLINKER_PCB)

        r3 = next(c for c in components if c.designator == "R3")
        assert r3.layer == Layer.BOTTOM


class TestExtractPads:
    """extract_pads関数のテスト."""

    def test_extracts_all_pads(self):
        pads = extract_pads(LED_BLINKER_PCB)

        assert len(pads) == 18

    def test_extracts_pad_attributes(self):
        pads = extract_pads(LED_BLINKER_PCB)

        u1_pad1 = next(p for p in pads if p.designator == "U1" and p.pad_number == "1")
        assert u1_pad1.net_name == "GND"
        assert u1_pad1.layer == Layer.TOP
        assert u1_pad1.is_custom_shape is False

    def test_extracts_bottom_layer_pad(self):
        pads = extract_pads(LED_BLINKER_PCB)

        r3_pads = [p for p in pads if p.designator == "R3"]
        assert len(r3_pads) == 2
        assert all(p.layer == Layer.BOTTOM for p in r3_pads)

    def test_pad_polygon_has_valid_area(self):
        pads = extract_pads(LED_BLINKER_PCB)

        for pad in pads:
            assert pad.area > 0

    def test_pad_center_is_within_polygon(self):
        pads = extract_pads(LED_BLINKER_PCB)

        for pad in pads:
            assert pad.polygon.contains(pad.polygon.centroid)
