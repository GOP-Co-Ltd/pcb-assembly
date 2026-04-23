from pathlib import Path

import pytest
from shapely import Polygon

from pcb_assembly.geometry.transform import Point2d
from pcb_assembly.pcb.board import (
    PNP_CSV_HEADER,
    Component,
    ComponentList,
    Copper,
    CopperList,
    Layer,
    Outline,
    Pad,
    PadList,
)


class TestOutline:
    """Outlineクラスのテスト."""

    @pytest.fixture
    def sample(self) -> Outline:
        # 20mm x 25mm の矩形基板
        return Outline(Polygon([(0, 0), (20, 0), (20, 25), (0, 25), (0, 0)]))

    def test_width_and_height(self, sample: Outline):
        assert sample.width == pytest.approx(20.0)
        assert sample.height == pytest.approx(25.0)

    def test_polygon(self, sample: Outline):
        assert sample.polygon.is_valid
        assert sample.polygon.area == pytest.approx(500.0)

    def test_save_and_load_roundtrip(self, sample: Outline, tmp_path: Path):
        json_path = tmp_path / "outline.json"
        sample.save(json_path)
        loaded = Outline.load(json_path)

        assert loaded.width == pytest.approx(sample.width)
        assert loaded.height == pytest.approx(sample.height)
        assert loaded.polygon.equals(sample.polygon)


class TestComponent:
    """Componentクラスのテスト."""

    @pytest.fixture
    def sample(self) -> Component:
        return Component(
            designator="U1",
            value="STM32F103",
            package="LQFP-48",
            position=Point2d(x=50.5, y=30.25),
            rotation=45.0,
            layer=Layer.TOP,
        )

    def test_from_csv_row(self):
        row = {
            "Designator": "R1",
            "Value": "10k",
            "Package": "0402",
            "X": "10.5000",
            "Y": "20.2500",
            "Rotation": "90.00",
            "Layer": "Bottom",
        }
        component = Component.from_csv_row(row)

        assert component.designator == "R1"
        assert component.value == "10k"
        assert component.package == "0402"
        assert component.position.x == pytest.approx(10.5)
        assert component.position.y == pytest.approx(20.25)
        assert component.rotation == pytest.approx(90.0)
        assert component.layer == Layer.BOTTOM

    def test_to_csv_row(self, sample: Component):
        row = sample.to_csv_row()

        assert row == [
            "U1",
            "STM32F103",
            "LQFP-48",
            "50.5000",
            "30.2500",
            "45.00",
            "Top",
        ]


class TestComponentList:
    """ComponentListクラスのテスト."""

    @pytest.fixture
    def sample_components(self) -> ComponentList:
        components = ComponentList()
        components.append(
            Component(
                designator="U1",
                value="STM32F103",
                package="LQFP-48",
                position=Point2d(x=50.5, y=30.25),
                rotation=45.0,
                layer=Layer.TOP,
            )
        )
        components.append(
            Component(
                designator="R1",
                value="10k",
                package="0402",
                position=Point2d(x=10.0, y=20.0),
                rotation=0.0,
                layer=Layer.BOTTOM,
            )
        )
        return components

    def test_save_and_load_roundtrip(
        self, sample_components: ComponentList, tmp_path: Path
    ):
        csv_path = tmp_path / "components.csv"
        sample_components.save(csv_path)
        loaded = ComponentList.load(csv_path)

        assert len(loaded) == 2
        assert loaded[0].designator == "U1"
        assert loaded[1].designator == "R1"

    def test_save_creates_csv_with_header(
        self, sample_components: ComponentList, tmp_path: Path
    ):
        csv_path = tmp_path / "components.csv"
        sample_components.save(csv_path)

        lines = csv_path.read_text().strip().split("\n")
        assert lines[0] == ",".join(PNP_CSV_HEADER)
        assert len(lines) == 3  # header + 2 components

    def test_list_operations(self, sample_components: ComponentList):
        assert len(sample_components) == 2
        assert sample_components[0].designator == "U1"

        new_component = Component(
            designator="C1",
            value="100nF",
            package="0402",
            position=Point2d(x=15.0, y=25.0),
            rotation=180.0,
            layer=Layer.TOP,
        )
        sample_components.append(new_component)
        assert len(sample_components) == 3

    def test_nearest(self, sample_components: ComponentList):
        # U1: (50.5, 30.25), R1: (10.0, 20.0)
        # (0, 0)に近いのはR1
        nearest = sample_components.nearest(Point2d(x=0.0, y=0.0))
        assert nearest.designator == "R1"

        # (100, 100)に近いのはU1
        nearest = sample_components.nearest(Point2d(x=100.0, y=100.0))
        assert nearest.designator == "U1"

    def test_nearest_empty_list_raises_error(self):
        empty = ComponentList()
        with pytest.raises(ValueError, match="ComponentList is empty"):
            empty.nearest(Point2d(x=0.0, y=0.0))


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
        assert center.x == pytest.approx(0.5)
        assert center.y == pytest.approx(0.5)

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

    def test_pad_polygon_with_holes_roundtrip(self):
        # 穴付きポリゴン (10x10 の外形に 2x2 の穴) でも往復できることを確認
        exterior = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
        hole = [(4, 4), (6, 4), (6, 6), (4, 6), (4, 4)]
        pad = Pad(
            designator="U1",
            pad_number="1",
            net_name="VCC",
            layer=Layer.TOP,
            polygon=Polygon(exterior, holes=[hole]),
        )

        restored = Pad.from_dict(pad.to_dict())

        assert restored.layer == pad.layer
        assert restored.polygon.equals(pad.polygon)
        assert len(list(restored.polygon.interiors)) == 1
        assert restored.polygon.area == pytest.approx(100.0 - 4.0)


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

    def test_nearest(self, sample_pads: PadList):
        # パッド1の中心: (0.5, 0.5), パッド2の中心: (2.5, 0.5)
        # (0, 0)に近いのはパッド1
        nearest = sample_pads.nearest(Point2d(x=0.0, y=0.0))
        assert nearest.pad_number == "1"

        # (10, 0)に近いのはパッド2
        nearest = sample_pads.nearest(Point2d(x=10.0, y=0.0))
        assert nearest.pad_number == "2"

    def test_nearest_empty_list_raises_error(self):
        empty = PadList()
        with pytest.raises(ValueError, match="PadList is empty"):
            empty.nearest(Point2d(x=0.0, y=0.0))


class TestCopper:
    """Copperクラスのテスト."""

    @pytest.mark.parametrize("layer", [Layer.TOP, Layer.BOTTOM])
    def test_copper_to_dict_from_dict_roundtrip(self, layer: Layer):
        exterior = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
        hole = [(4, 4), (6, 4), (6, 6), (4, 6), (4, 4)]
        copper = Copper(
            layer=layer,
            polygon=Polygon(exterior, holes=[hole]),
        )

        restored = Copper.from_dict(copper.to_dict())

        assert restored.layer == copper.layer
        assert restored.polygon.equals(copper.polygon)
        assert restored.area == pytest.approx(copper.area)
        assert restored.area == pytest.approx(100.0 - 4.0)

    def test_area(self):
        copper = Copper(
            layer=Layer.TOP,
            polygon=Polygon([(0, 0), (5, 0), (5, 4), (0, 4), (0, 0)]),
        )
        assert copper.area == pytest.approx(20.0)


class TestCopperList:
    """CopperListクラスのテスト."""

    def test_save_and_load_roundtrip(self, tmp_path: Path):
        coppers = CopperList(
            [
                Copper(
                    layer=Layer.TOP,
                    polygon=Polygon(
                        [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)],
                        holes=[[(4, 4), (6, 4), (6, 6), (4, 6), (4, 4)]],
                    ),
                ),
                Copper(
                    layer=Layer.BOTTOM,
                    polygon=Polygon([(20, 20), (25, 20), (25, 25), (20, 25), (20, 20)]),
                ),
            ]
        )
        json_path = tmp_path / "copper.json"
        coppers.save(json_path)
        loaded = CopperList.load(json_path)

        assert len(loaded) == len(coppers)
        for original, restored in zip(coppers, loaded, strict=True):
            assert restored.layer == original.layer
            assert restored.polygon.equals(original.polygon)
