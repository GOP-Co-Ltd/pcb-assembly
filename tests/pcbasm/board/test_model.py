import pytest
from shapely import Polygon

from pcbasm.board import Outline


class TestOutline:
    """Outlineクラスのテスト."""

    def test_width_and_height(self):
        # 20mm x 25mm の矩形基板
        outline = Outline(Polygon([(0, 0), (20, 0), (20, 25), (0, 25), (0, 0)]))

        assert outline.width == pytest.approx(20.0)
        assert outline.height == pytest.approx(25.0)
