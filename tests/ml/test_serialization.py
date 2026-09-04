"""暗黙変換を許さない cattrs converter の公開契約."""

from pathlib import Path
from typing import Literal

import attrs
import pytest

from ml.serialization import make_strict_converter, structure_strictly


@attrs.frozen
class Scalars:
    count: int
    ratio: float
    name: str
    enabled: bool


@attrs.frozen
class Shapes:
    mode: Literal["train", "evaluate"]
    channels: tuple[int, ...]
    size: tuple[int, int]
    output: Path


def _scalars_payload() -> dict[str, object]:
    return {"count": 3, "ratio": 0.5, "name": "base", "enabled": True}


def _shapes_payload() -> dict[str, object]:
    return {
        "mode": "train",
        "channels": [24, 32, 48],
        "size": [512, 256],
        "output": "/data/run",
    }


class TestStrictScalars:
    """数値と真偽値の暗黙変換を拒否する."""

    def test_structures_exactly_typed_values(self):
        value, error = structure_strictly(
            _scalars_payload(), Scalars, converter=make_strict_converter()
        )

        assert error is None
        assert value == Scalars(count=3, ratio=0.5, name="base", enabled=True)

    @pytest.mark.parametrize(
        ("field", "wrong_value"),
        [
            ("count", 1.0),
            ("count", True),
            ("count", "3"),
            ("ratio", 1),
            ("ratio", "0.5"),
            ("name", 3),
            ("enabled", 1),
            ("enabled", "true"),
        ],
    )
    def test_rejects_implicitly_convertible_values(self, field: str, wrong_value):
        payload = _scalars_payload()
        payload[field] = wrong_value

        value, error = structure_strictly(
            payload, Scalars, converter=make_strict_converter()
        )

        assert value is None
        assert error is not None
        assert field in error

    def test_rejects_unknown_keys(self):
        payload = _scalars_payload() | {"extra": 1}

        value, error = structure_strictly(
            payload, Scalars, converter=make_strict_converter()
        )

        assert value is None
        assert error is not None
        assert "extra" in error

    def test_reports_a_missing_key(self):
        payload = _scalars_payload()
        del payload["ratio"]

        value, error = structure_strictly(
            payload, Scalars, converter=make_strict_converter()
        )

        assert value is None
        assert error is not None
        assert "ratio" in error


class TestStrictShapes:
    """Literal・tuple・Path の構造化."""

    def test_structures_literal_tuple_and_path(self):
        value, error = structure_strictly(
            _shapes_payload(), Shapes, converter=make_strict_converter()
        )

        assert error is None
        assert value == Shapes(
            mode="train",
            channels=(24, 32, 48),
            size=(512, 256),
            output=Path("/data/run"),
        )

    @pytest.mark.parametrize(
        ("field", "wrong_value"),
        [
            ("mode", "export"),
            ("channels", [24, "32"]),
            ("size", [1, 2, 3]),
            ("output", 5),
        ],
    )
    def test_rejects_values_outside_the_declared_shape(self, field: str, wrong_value):
        payload = _shapes_payload()
        payload[field] = wrong_value

        value, error = structure_strictly(
            payload, Shapes, converter=make_strict_converter()
        )

        assert value is None
        assert error is not None
        assert field in error


class TestUnstructure:
    """JSON へ書き出せる素の値へ戻す."""

    def test_unstructures_tuple_and_path_to_json_types(self):
        payload = make_strict_converter().unstructure(
            Shapes(
                mode="train",
                channels=(24, 32),
                size=(512, 256),
                output=Path("/data/run"),
            )
        )

        assert payload == {
            "mode": "train",
            "channels": [24, 32],
            "size": [512, 256],
            "output": "/data/run",
        }

    def test_round_trips_through_unstructure_and_structure(self):
        converter = make_strict_converter()
        original = Shapes(
            mode="evaluate",
            channels=(48,),
            size=(64, 64),
            output=Path("/data/run"),
        )

        value, error = structure_strictly(
            converter.unstructure(original), Shapes, converter=converter
        )

        assert error is None
        assert value == original
