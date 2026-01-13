from pathlib import Path

import pytest
from shapely import Polygon

from pcb_assembly.pcb.pad import Pad, PadList
from pcb_assembly.pcb.utils import Layer


class TestPad:
    """Padクラスのテスト."""

    @pytest.fixture
    def sample(self) -> Pad:
        # 1mm x 1mm の正方形パッド (中心が0.5, 0.5)
        return Pad(
            designator="U1",
            pad_number="1",
            net_name="VCC",
            layer=Layer.TOP,
            polygon=Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]),
        )

    def test_center(self, sample: Pad):
        center = sample.center
        assert center[0] == pytest.approx(0.5)
        assert center[1] == pytest.approx(0.5)

    def test_area(self, sample: Pad):
        assert sample.area == pytest.approx(1.0)

    def test_to_dict_and_from_dict_roundtrip(self, sample: Pad):
        data = sample.to_dict()
        restored = Pad.from_dict(data)

        assert restored.designator == sample.designator
        assert restored.pad_number == sample.pad_number
        assert restored.net_name == sample.net_name
        assert restored.layer == sample.layer
        assert restored.is_custom_shape == sample.is_custom_shape
        assert restored.polygon.equals(sample.polygon)


class TestPadList:
    """PadListクラスのテスト."""

    @pytest.fixture
    def sample_pads(self) -> PadList:
        pads = PadList()
        pads.append(
            Pad(
                designator="U1",
                pad_number="1",
                net_name="VCC",
                layer=Layer.TOP,
                polygon=Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]),
            )
        )
        pads.append(
            Pad(
                designator="U1",
                pad_number="2",
                net_name="GND",
                layer=Layer.TOP,
                polygon=Polygon([(2, 0), (3, 0), (3, 1), (2, 1), (2, 0)]),
                is_custom_shape=True,
            )
        )
        return pads

    def test_save_and_load_roundtrip(self, sample_pads: PadList, tmp_path: Path):
        json_path = tmp_path / "pads.json"
        sample_pads.save(json_path)
        loaded = PadList.load(json_path)

        assert len(loaded) == 2
        assert loaded[0].designator == "U1"
        assert loaded[0].pad_number == "1"
        assert loaded[1].pad_number == "2"
        assert loaded[1].is_custom_shape is True

    def test_list_operations(self, sample_pads: PadList):
        assert len(sample_pads) == 2
        assert sample_pads[0].pad_number == "1"

        new_pad = Pad(
            designator="R1",
            pad_number="1",
            net_name="NET1",
            layer=Layer.BOTTOM,
            polygon=Polygon([(5, 5), (6, 5), (6, 6), (5, 6), (5, 5)]),
        )
        sample_pads.append(new_pad)
        assert len(sample_pads) == 3
