"""mirror_model: attrs クラス → 同形 pydantic モデルの生成契約."""

from typing import Literal

import attrs
import pytest
from pydantic import ValidationError

from web.api.attrs_models import mirror_model


@attrs.frozen
class _Point:
    x: float
    y: float


@attrs.frozen
class _Shape:
    kind: Literal["circle", "rect"]
    points: tuple[_Point, ...]
    label: str = ""
    radius: float | None = None


@attrs.frozen
class _Sheet:
    shapes: tuple[_Shape, ...] = attrs.Factory(tuple)
    origin: _Point = attrs.Factory(lambda: _Point(0.0, 0.0))


class TestMirrorModel:
    def test_round_trips_attrs_instance_to_same_json_shape(self):
        sheet = _Sheet(
            shapes=(_Shape("rect", (_Point(1.0, 2.0), _Point(3.0, 4.0)), "a", 0.5),),
            origin=_Point(5.0, 6.0),
        )

        model = mirror_model(_Sheet).model_validate(sheet, strict=False)

        assert model.model_dump() == {
            "shapes": [
                {
                    "kind": "rect",
                    "points": [{"x": 1.0, "y": 2.0}, {"x": 3.0, "y": 4.0}],
                    "label": "a",
                    "radius": 0.5,
                }
            ],
            "origin": {"x": 5.0, "y": 6.0},
        }

    def test_scalar_defaults_are_kept_and_factory_defaults_become_required(self):
        model = mirror_model(_Shape)
        parsed = model.model_validate({"kind": "circle", "points": []})
        assert parsed.model_dump() == {
            "kind": "circle",
            "points": [],
            "label": "",
            "radius": None,
        }

        with pytest.raises(ValidationError, match="shapes"):
            mirror_model(_Sheet).model_validate({"origin": {"x": 0.0, "y": 0.0}})

    def test_forbids_unknown_keys_and_wrong_literal(self):
        model = mirror_model(_Shape)

        with pytest.raises(ValidationError, match="extra"):
            model.model_validate({"kind": "circle", "points": [], "bogus": 1})
        with pytest.raises(ValidationError, match="kind"):
            model.model_validate({"kind": "triangle", "points": []})

    def test_same_attrs_class_resolves_to_same_model(self):
        assert mirror_model(_Point) is mirror_model(_Point)
        nested = mirror_model(_Shape).model_fields["points"].annotation
        assert nested == list[mirror_model(_Point)]

    def test_rejects_non_attrs_and_unsupported_annotations(self):
        class Plain:
            pass

        @attrs.frozen
        class _Unsupported:
            values: dict[str, int]

        with pytest.raises(TypeError, match="attrs"):
            mirror_model(Plain)
        with pytest.raises(TypeError, match="values"):
            mirror_model(_Unsupported)
