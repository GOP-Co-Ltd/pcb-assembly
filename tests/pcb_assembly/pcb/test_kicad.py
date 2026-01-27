import pytest

from pcb_assembly.pcb import Layer, PcbFile
from tests.helpers import TESTING_DATA_DIR

LED_BLINKER_PCB = TESTING_DATA_DIR / "led_blinker" / "led_blinker.kicad_pcb"


class TestPcbFile:
    """PcbFileクラスのテスト."""

    @pytest.fixture
    def pcb(self):
        return PcbFile(LED_BLINKER_PCB)

    # outline プロパティ

    def test_outline_extracts_board_size(self, pcb: PcbFile):
        assert pcb.outline.width == pytest.approx(20.0, abs=0.1)
        assert pcb.outline.height == pytest.approx(25.0, abs=0.1)

    def test_outline_extracts_valid_polygon(self, pcb: PcbFile):
        assert pcb.outline.polygon.is_valid
        assert pcb.outline.polygon.area > 0

    # components プロパティ

    def test_components_extracts_all_components(self, pcb: PcbFile):
        assert len(pcb.components) == 7

    def test_components_extracts_component_attributes(self, pcb: PcbFile):
        u1 = next(c for c in pcb.components if c.designator == "U1")
        assert u1.value == "ATtiny85"
        assert u1.package == "SOT-23-6"
        assert u1.position.x == pytest.approx(10.0)
        assert u1.position.y == pytest.approx(10.0)
        assert u1.rotation == pytest.approx(0.0)
        assert u1.layer == Layer.TOP

    def test_components_extracts_rotated_component(self, pcb: PcbFile):
        d1 = next(c for c in pcb.components if c.designator == "D1")
        assert d1.rotation == pytest.approx(90.0)

    def test_components_extracts_bottom_layer_component(self, pcb: PcbFile):
        r3 = next(c for c in pcb.components if c.designator == "R3")
        assert r3.layer == Layer.BOTTOM

    # pads プロパティ

    def test_pads_extracts_all_pads(self, pcb: PcbFile):
        assert len(pcb.pads) == 18

    def test_pads_extracts_pad_attributes(self, pcb: PcbFile):
        u1_pad1 = next(
            p for p in pcb.pads if p.designator == "U1" and p.pad_number == "1"
        )
        assert u1_pad1.net_name == "GND"
        assert u1_pad1.layer == Layer.TOP
        assert u1_pad1.is_custom_shape is False

    def test_pads_extracts_bottom_layer_pad(self, pcb: PcbFile):
        r3_pads = [p for p in pcb.pads if p.designator == "R3"]
        assert len(r3_pads) == 2
        assert all(p.layer == Layer.BOTTOM for p in r3_pads)

    def test_pads_polygon_has_valid_area(self, pcb: PcbFile):
        for pad in pcb.pads:
            assert pad.area > 0

    def test_pads_center_is_within_polygon(self, pcb: PcbFile):
        for pad in pcb.pads:
            assert pad.polygon.contains(pad.polygon.centroid)
