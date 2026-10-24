import pytest
from shapely import Polygon

from pcbasm.board import Layer, Outline, Pad


class TestOutline:
    """Outlineクラスのテスト."""

    def test_width_and_height(self):
        # 20mm x 25mm の矩形基板
        outline = Outline(Polygon([(0, 0), (20, 0), (20, 25), (0, 25), (0, 0)]))

        assert outline.width == pytest.approx(20.0)
        assert outline.height == pytest.approx(25.0)


class TestPad:
    def test_ref_defaults_to_designator_and_pad_number(self):
        pad = Pad("U1", "3", "GND", Layer.TOP, Polygon([(0, 0), (1, 0), (1, 1)]))

        assert pad.ref == "U1.3"
