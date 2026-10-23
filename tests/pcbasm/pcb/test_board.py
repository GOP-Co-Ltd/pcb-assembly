import json
from pathlib import Path

import pytest
from shapely import Polygon

from pcbasm.geometry.transform import Point2d
from pcbasm.pcb.board import (
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

    def test_save_writes_exterior_coords(self, sample: Outline, tmp_path: Path):
        json_path = tmp_path / "outline.json"
        sample.save(json_path)

        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert Polygon(data["polygon"]).equals(sample.polygon)


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

    def test_save_creates_csv_with_header(
        self, sample_components: ComponentList, tmp_path: Path
    ):
        csv_path = tmp_path / "components.csv"
        sample_components.save(csv_path)

        lines = csv_path.read_text().strip().split("\n")
        assert lines[0] == ",".join(PNP_CSV_HEADER)
        assert len(lines) == 3  # header + 2 components


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

    def test_to_dict_keeps_polygon_holes(self):
        # 穴付きポリゴン (10x10 の外形に 2x2 の穴) の穴も書き出す
        exterior = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
        hole = [(4, 4), (6, 4), (6, 6), (4, 6), (4, 4)]
        pad = Pad(
            designator="U1",
            pad_number="1",
            net_name="VCC",
            layer=Layer.TOP,
            polygon=Polygon(exterior, holes=[hole]),
        )

        data = pad.to_dict()

        assert data["layer"] == "Top"
        assert Polygon(data["polygon"]["exterior"], data["polygon"]["holes"]).equals(
            pad.polygon
        )


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

    def test_save_writes_each_pad(self, sample_pads: PadList, tmp_path: Path):
        json_path = tmp_path / "pads.json"
        sample_pads.save(json_path)

        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert [(d["designator"], d["pad_number"]) for d in data] == [
            ("U1", "1"),
            ("U1", "2"),
        ]
        assert data[1]["is_custom_shape"] is True


class TestCopperList:
    """CopperListクラスのテスト."""

    def test_save_writes_layer_and_polygon_with_holes(self, tmp_path: Path):
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

        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert [d["layer"] for d in data] == ["Top", "Bottom"]
        for original, written in zip(coppers, data, strict=True):
            polygon = written["polygon"]
            assert Polygon(polygon["exterior"], polygon["holes"]).equals(
                original.polygon
            )
