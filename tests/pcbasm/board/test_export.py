import csv
import json
from pathlib import Path

from shapely import Polygon

from pcbasm.board import Component, Copper, Layer, Outline, Pad
from pcbasm.board.export import (
    PNP_CSV_HEADER,
    save_copper,
    save_outline,
    save_pads,
    save_pnp_csv,
)
from pcbasm.geometry.transform import Point2d


class TestSaveOutline:
    def test_writes_exterior_coords(self, tmp_path: Path):
        outline = Outline(Polygon([(0, 0), (20, 0), (20, 25), (0, 25), (0, 0)]))
        json_path = tmp_path / "outline.json"

        save_outline(outline, json_path)

        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert Polygon(data["polygon"]).equals(outline.polygon)


class TestSavePnpCsv:
    def test_writes_header_and_one_row_per_component(self, tmp_path: Path):
        components = [
            Component(
                designator="U1",
                value="STM32F103",
                package="LQFP-48",
                position=Point2d(x=50.5, y=30.25),
                rotation=45.0,
                layer=Layer.TOP,
            ),
            Component(
                designator="R1",
                value="10k",
                package="0402",
                position=Point2d(x=10.0, y=20.0),
                rotation=0.0,
                layer=Layer.BOTTOM,
            ),
        ]
        csv_path = tmp_path / "components.csv"

        save_pnp_csv(components, csv_path)

        with csv_path.open(encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        assert rows == [
            list(PNP_CSV_HEADER),
            ["U1", "STM32F103", "LQFP-48", "50.5000", "30.2500", "45.00", "Top"],
            ["R1", "10k", "0402", "10.0000", "20.0000", "0.00", "Bottom"],
        ]


class TestSavePads:
    def test_writes_each_pad(self, tmp_path: Path):
        pads = [
            Pad(
                designator="U1",
                pad_number="1",
                net_name="VCC",
                layer=Layer.TOP,
                polygon=Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]),
            ),
            Pad(
                designator="U1",
                pad_number="2",
                net_name="GND",
                layer=Layer.TOP,
                polygon=Polygon([(2, 0), (3, 0), (3, 1), (2, 1), (2, 0)]),
                is_custom_shape=True,
            ),
        ]
        json_path = tmp_path / "pads.json"

        save_pads(pads, json_path)

        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert [(d["designator"], d["pad_number"]) for d in data] == [
            ("U1", "1"),
            ("U1", "2"),
        ]
        assert data[1]["is_custom_shape"] is True

    def test_keeps_polygon_holes(self, tmp_path: Path):
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
        json_path = tmp_path / "pads.json"

        save_pads([pad], json_path)

        (data,) = json.loads(json_path.read_text(encoding="utf-8"))
        assert data["layer"] == "Top"
        assert Polygon(data["polygon"]["exterior"], data["polygon"]["holes"]).equals(
            pad.polygon
        )


class TestSaveCopper:
    def test_writes_layer_and_polygon_with_holes(self, tmp_path: Path):
        coppers = [
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
        json_path = tmp_path / "copper.json"

        save_copper(coppers, json_path)

        data = json.loads(json_path.read_text(encoding="utf-8"))
        assert [d["layer"] for d in data] == ["Top", "Bottom"]
        for original, written in zip(coppers, data, strict=True):
            polygon = written["polygon"]
            assert Polygon(polygon["exterior"], polygon["holes"]).equals(
                original.polygon
            )
