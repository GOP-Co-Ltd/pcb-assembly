from pathlib import Path

import pcbnew
import pytest
from shapely.geometry import Point as ShapelyPoint

from pcbasm.pcb import Layer, PcbFile
from tests.helpers import TESTING_DATA_DIR

LED_BLINKER_PCB = TESTING_DATA_DIR / "led_blinker" / "led_blinker.kicad_pcb"
VIA_CENTERS = (
    (11.1, 11.7),
    (4.49, 11.7),
    (2.0, 2.0),
    (18.0, 23.0),
)


def _add_edge_cuts_rectangle(
    board: pcbnew.BOARD, corners: list[tuple[float, float]]
) -> None:
    """実pcbnew boardのEdge.Cutsへ閉じた矩形を追加する."""
    for start, end in zip(corners, corners[1:] + corners[:1], strict=True):
        segment = pcbnew.PCB_SHAPE(board)
        segment.SetShape(pcbnew.SHAPE_T_SEGMENT)
        segment.SetLayer(pcbnew.Edge_Cuts)
        segment.SetStart(
            pcbnew.VECTOR2I(pcbnew.FromMM(start[0]), pcbnew.FromMM(start[1]))
        )
        segment.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(end[0]), pcbnew.FromMM(end[1])))
        segment.SetWidth(pcbnew.FromMM(0.1))
        board.Add(segment)


class TestPcbFile:
    """PcbFileクラスのテスト."""

    @pytest.fixture
    def pcb(self):
        return PcbFile(LED_BLINKER_PCB)

    # outline プロパティ

    def test_outline_extracts_board_size(self, pcb: PcbFile):
        assert pcb.outline.width == pytest.approx(20.0, abs=0.1)
        assert pcb.outline.height == pytest.approx(25.0, abs=0.1)

    def test_outline_preserves_edge_cuts_hole(self, tmp_path: Path):
        board = pcbnew.BOARD()
        _add_edge_cuts_rectangle(
            board,
            [(10.0, 20.0), (50.0, 20.0), (50.0, 60.0), (10.0, 60.0)],
        )
        _add_edge_cuts_rectangle(
            board,
            [(20.0, 30.0), (30.0, 30.0), (30.0, 40.0), (20.0, 40.0)],
        )
        path = tmp_path / "outline-with-hole.kicad_pcb"
        pcbnew.SaveBoard(str(path), board)

        outline = PcbFile(path).outline

        assert outline.width == pytest.approx(40.0)
        assert outline.height == pytest.approx(40.0)
        assert outline.polygon.area == pytest.approx(1500.0)
        assert len(outline.polygon.interiors) == 1
        assert outline.polygon.covers(ShapelyPoint(5.0, 5.0))
        assert not outline.polygon.covers(ShapelyPoint(15.0, 15.0))

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

    def test_pads_center_is_within_polygon(self, pcb: PcbFile):
        for pad in pcb.pads:
            assert pad.polygon.contains(pad.polygon.centroid)

    def test_pads_copper_polygon_covers_paste_centroid(self, pcb: PcbFile):
        # 各 paste pad の copper_polygon は実銅箔形状（GetEffectivePolygon）。
        # valid・非空で、ペースト開口の中心を銅箔が覆う（銅箔 ⊇ paste の近似ピン）
        for pad in pcb.pads:
            assert pad.copper_polygon.is_valid, f"{pad.designator}.{pad.pad_number}"
            assert not pad.copper_polygon.is_empty
            assert pad.copper_polygon.area > 0
            assert pad.copper_polygon.covers(
                pad.polygon.centroid
            ), f"{pad.designator}.{pad.pad_number} の銅箔が paste 中心を覆っていない"

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

    @pytest.mark.parametrize("layer", [Layer.TOP, Layer.BOTTOM])
    def test_copper_excludes_via_drill_holes(self, pcb: PcbFile, layer: Layer):
        layer_copper = [c.polygon for c in pcb.copper if c.layer == layer]

        for x, y in VIA_CENTERS:
            center = ShapelyPoint(x, y)
            assert not any(polygon.covers(center) for polygon in layer_copper)
            assert min(polygon.distance(center) for polygon in layer_copper) == (
                pytest.approx(0.2, abs=0.02)
            )

    def test_copper_contains_track_connected_pad_centroids(self, pcb: PcbFile):
        # LED1 ネットは TOP 層で U1.2 -> R1.1, R1.2 -> D1.1 をトラックで結線している
        # 同一ネットでも R1 本体で分断されるため、銅箔島は 2 つに分かれる
        led1_pads = {
            (p.designator, p.pad_number): p
            for p in pcb.pads
            if p.net_name == "LED1" and p.layer == Layer.TOP
        }
        top_copper = [c for c in pcb.copper if c.layer == Layer.TOP]

        for left, right in [(("U1", "2"), ("R1", "1")), (("R1", "2"), ("D1", "1"))]:
            left_pad = led1_pads[left]
            right_pad = led1_pads[right]
            island = next(
                c for c in top_copper if c.polygon.contains(left_pad.polygon.centroid)
            )
            assert island.polygon.contains(
                right_pad.polygon.centroid
            ), f"{right[0]}.{right[1]} は {left[0]}.{left[1]} と同じ銅箔島に含まれていない"
