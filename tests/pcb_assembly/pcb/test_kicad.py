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

    # copper プロパティ

    def test_copper_exists_on_both_layers(self, pcb: PcbFile):
        top = [c for c in pcb.copper if c.layer == Layer.TOP]
        bottom = [c for c in pcb.copper if c.layer == Layer.BOTTOM]
        assert len(top) >= 1
        assert len(bottom) >= 1

    def test_copper_polygons_are_valid(self, pcb: PcbFile):
        for copper in pcb.copper:
            assert copper.polygon.is_valid
            assert copper.area > 0

    def test_copper_polygons_normalized(self, pcb: PcbFile):
        # 浮動小数点誤差を許容するための ε
        eps = 0.01
        max_x = pcb.outline.width + eps
        max_y = pcb.outline.height + eps
        for copper in pcb.copper:
            minx, miny, maxx, maxy = copper.polygon.bounds
            assert minx >= -eps
            assert miny >= -eps
            assert maxx <= max_x
            assert maxy <= max_y

    def test_copper_contains_connected_pad_centroids(self, pcb: PcbFile):
        # LED1 ネットは TOP 層で U1.2, R1.1, R1.2, D1.1 を接続している
        led1_pads = [
            p for p in pcb.pads if p.net_name == "LED1" and p.layer == Layer.TOP
        ]
        u1_pad2 = next(p for p in led1_pads if p.designator == "U1")

        top_copper = [c for c in pcb.copper if c.layer == Layer.TOP]
        containing = next(
            c for c in top_copper if c.polygon.contains(u1_pad2.polygon.centroid)
        )

        for pad in led1_pads:
            assert containing.polygon.contains(
                pad.polygon.centroid
            ), f"{pad.designator}.{pad.pad_number} は同じ銅箔島に含まれていない"
