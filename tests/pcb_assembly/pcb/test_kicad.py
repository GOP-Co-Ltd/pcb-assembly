import pytest

from pcb_assembly.pcb import Layer, extract_components, extract_outline, extract_pads
from tests.helpers import TESTING_DATA_DIR

LED_BLINKER_PCB = TESTING_DATA_DIR / "led_blinker" / "led_blinker.kicad_pcb"


class TestExtractOutline:
    """extract_outline関数のテスト."""

    def test_extracts_board_size(self):
        outline = extract_outline(LED_BLINKER_PCB)

        assert outline.width == pytest.approx(20.0, abs=0.1)
        assert outline.height == pytest.approx(25.0, abs=0.1)

    def test_extracts_valid_polygon(self):
        outline = extract_outline(LED_BLINKER_PCB)

        assert outline.polygon.is_valid
        assert outline.polygon.area > 0


class TestExtractComponents:
    """extract_components関数のテスト."""

    def test_extracts_all_components(self):
        components = extract_components(LED_BLINKER_PCB)

        assert len(components) == 7

    def test_extracts_component_attributes(self):
        components = extract_components(LED_BLINKER_PCB)

        # 座標は基板左上を原点として正規化される
        # 元の座標 (105, 100)、基板左上 (95, 90) → 正規化後 (10, 10)
        u1 = next(c for c in components if c.designator == "U1")
        assert u1.value == "ATtiny85"
        assert u1.package == "SOT-23-6"
        assert u1.position.x == pytest.approx(10.0)
        assert u1.position.y == pytest.approx(10.0)
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
