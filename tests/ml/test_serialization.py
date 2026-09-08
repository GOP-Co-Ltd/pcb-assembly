"""暗黙変換を許さない cattrs converter の公開契約."""

from collections.abc import Mapping
from enum import IntEnum
from pathlib import Path
from typing import Literal

import attrs
import pytest

from ml.experiment.logger import Scalar
from ml.serialization import make_strict_converter, structure_strictly


class _Level(IntEnum):
    """``int`` の部分型。scalar として通ってはならない."""

    LOW = 1


@attrs.frozen
class _ScalarContainers:
    """値ごとに型が違う scalar の集まりを持つ記録."""

    parameters: Mapping[str, Scalar]
    choices: tuple[Scalar, ...]


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


class TestScalarUnion:
    """``Scalar`` の union を、部分型を混ぜずに構造化する.

    ``Mapping[str, Scalar]`` と ``tuple[Scalar, ...]`` は、探索した param の記録
    （``ml.tuning.study.TrialRecord``）と categorical 分布の候補
    （``ml.tuning.search_space.ParameterDistribution``）が要求する形。

    hook が無いと cattrs が ``Unsupported type`` で拒否し、成果物を読み戻せない。
    """

    def test_structures_scalar_containers(self):
        value, error = structure_strictly(
            {
                "parameters": {"ratio": 0.5, "count": 3, "name": "base", "flag": True},
                "choices": ["min", "max"],
            },
            _ScalarContainers,
            converter=make_strict_converter(),
        )

        assert error is None
        assert value is not None
        assert value.choices == ("min", "max")

    @pytest.mark.parametrize(
        ("name", "value"),
        [("count", 3), ("ratio", 0.5), ("name", "base"), ("flag", True)],
    )
    def test_keeps_the_exact_scalar_type(self, name: str, value: object):
        structured, error = structure_strictly(
            {"parameters": {name: value}, "choices": []},
            _ScalarContainers,
            converter=make_strict_converter(),
        )

        assert error is None
        assert structured is not None
        # ``bool`` は ``int`` の部分型なので、値の一致では区別できない
        assert type(structured.parameters[name]) is type(value)

    def test_round_trips_without_promoting_integers_to_floats(self):
        converter = make_strict_converter()
        original = _ScalarContainers(
            parameters={"count": 3, "ratio": 0.5, "flag": True}, choices=(1, "a")
        )

        value, error = structure_strictly(
            converter.unstructure(original), _ScalarContainers, converter=converter
        )

        assert error is None
        assert value == original
        assert value is not None
        assert type(value.parameters["count"]) is int
        assert type(value.parameters["flag"]) is bool

    def test_rejects_a_subtype_of_int(self):
        """``IntEnum`` は ``int`` の部分型だが scalar として通してはならない.

        これを通すと「記録した値と実際に使われた値が食い違わない」という ``_exact_type`` の意図が崩れる。
        """

        value, error = structure_strictly(
            {"parameters": {"level": _Level.LOW}, "choices": []},
            _ScalarContainers,
            converter=make_strict_converter(),
        )

        assert value is None
        assert error is not None
        assert "level" in error

    @pytest.mark.parametrize("wrong_value", [[1], {"a": 1}, None])
    def test_rejects_values_that_are_not_scalars(self, wrong_value: object):
        value, error = structure_strictly(
            {"parameters": {"nested": wrong_value}, "choices": []},
            _ScalarContainers,
            converter=make_strict_converter(),
        )

        assert value is None
        assert error is not None
        assert "nested" in error
