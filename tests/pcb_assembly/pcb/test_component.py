from pathlib import Path

import pytest

from pcb_assembly.pcb.component import PNP_CSV_HEADER, Component, ComponentList, Layer


class TestComponent:
    """Componentクラスのテスト."""

    @pytest.fixture
    def sample(self) -> Component:
        return Component(
            designator="U1",
            value="STM32F103",
            package="LQFP-48",
            x=50.5,
            y=30.25,
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
        assert component.x == pytest.approx(10.5)
        assert component.y == pytest.approx(20.25)
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
                x=50.5,
                y=30.25,
                rotation=45.0,
                layer=Layer.TOP,
            )
        )
        components.append(
            Component(
                designator="R1",
                value="10k",
                package="0402",
                x=10.0,
                y=20.0,
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
            x=15.0,
            y=25.0,
            rotation=180.0,
            layer=Layer.TOP,
        )
        sample_components.append(new_component)
        assert len(sample_components) == 3
