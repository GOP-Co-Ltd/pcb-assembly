from __future__ import annotations

from typing import Literal

import attrs
import pytest

from pcbasm.document import omittable, parse_document, structure_document


@attrs.frozen
class _Numbers:
    count: int
    ratio: float
    flag: bool


@attrs.frozen
class _Optional:
    name: str
    note: str | None = omittable(None)
    unit: str = "mm"


@attrs.frozen
class _Containers:
    point: tuple[float, float]
    points: tuple[tuple[float, float], ...]
    values: dict[str, float]
    mode: Literal["dot", "line", 1]


@attrs.frozen
class _Unions:
    int_first: int | float
    float_first: float | int


@attrs.frozen
class _Choices:
    height: float | Literal["auto"]
    version: Literal[1]
    note: str | None


@attrs.frozen
class _Document:
    kind: Literal["sample"]
    schema_version: Literal[2]
    value: float


def _numbers(**changes: object) -> dict[str, object]:
    return {"count": 1, "ratio": 0.5, "flag": True, **changes}


def _containers(**changes: object) -> dict[str, object]:
    return {
        "point": [1.0, 2.0],
        "points": [[1.0, 2.0], [3.0, 4.0]],
        "values": {"a": 1.0},
        "mode": "dot",
        **changes,
    }


def _document(**changes: object) -> dict[str, object]:
    return {"kind": "sample", "schema_version": 2, "value": 1.5, **changes}


def _parse(data: dict[str, object]) -> tuple[_Document | None, str | None]:
    return parse_document(data, _Document, kind="sample", version=2, label="見本")


class TestStructureDocument:
    def test_restores_values_of_the_declared_types(self):
        assert structure_document(_numbers(), _Numbers) == _Numbers(1, 0.5, True)

    @pytest.mark.parametrize(
        "changes",
        [
            pytest.param({"count": True}, id="bool は int ではない"),
            pytest.param({"count": 1.0}, id="1.0 は int ではない"),
            pytest.param({"count": "1"}, id="文字列は int ではない"),
            pytest.param({"ratio": True}, id="bool は float ではない"),
            pytest.param({"ratio": 1}, id="int は float ではない"),
            pytest.param({"ratio": float("nan")}, id="nan"),
            pytest.param({"ratio": float("inf")}, id="inf"),
            pytest.param({"flag": 1}, id="1 は bool ではない"),
        ],
    )
    def test_rejects_implicit_conversions(self, changes: dict[str, object]):
        with pytest.raises(ValueError):
            structure_document(_numbers(**changes), _Numbers)

    def test_int_as_float_accepts_an_int_in_a_float_field_as_float(self):
        restored = structure_document(_numbers(ratio=40), _Numbers, int_as_float=True)

        assert restored.ratio == 40.0
        assert type(restored.ratio) is float

    def test_int_as_float_still_rejects_a_bool_in_a_float_field(self):
        with pytest.raises(ValueError):
            structure_document(_numbers(ratio=True), _Numbers, int_as_float=True)

    def test_rejects_unknown_keys_by_name(self):
        with pytest.raises(ValueError, match="未知のキーです: extra"):
            structure_document(_numbers(extra=1), _Numbers)

    def test_rejects_missing_keys_by_name(self):
        data = _numbers()
        del data["ratio"]

        with pytest.raises(ValueError, match="必須の項目がありません: ratio"):
            structure_document(data, _Numbers)

    def test_an_omittable_key_may_be_absent(self):
        restored = structure_document({"name": "a", "unit": "mm"}, _Optional)

        assert restored == _Optional(name="a", note=None)

    def test_a_default_alone_does_not_make_a_key_omittable(self):
        """既定値を持つ field も、omittable でなければ書かれていることを求める."""
        with pytest.raises(ValueError, match="必須の項目がありません: unit"):
            structure_document({"name": "a"}, _Optional)

    def test_restores_containers(self):
        restored = structure_document(_containers(), _Containers)

        assert restored == _Containers(
            point=(1.0, 2.0),
            points=((1.0, 2.0), (3.0, 4.0)),
            values={"a": 1.0},
            mode="dot",
        )

    @pytest.mark.parametrize(
        "changes",
        [
            pytest.param({"point": [1.0]}, id="固定長 tuple の要素数"),
            pytest.param({"point": {"x": 1.0}}, id="array でない tuple"),
            pytest.param({"points": [[1.0, "2"]]}, id="可変長 tuple の要素の型"),
            pytest.param({"values": "a"}, id="object でない dict"),
            pytest.param({"values": {"a": 1}}, id="dict の値の型"),
            pytest.param({"mode": "area"}, id="Literal にない値"),
            pytest.param({"mode": True}, id="Literal は型も一致させる"),
        ],
    )
    def test_rejects_malformed_containers(self, changes: dict[str, object]):
        with pytest.raises(ValueError):
            structure_document(_containers(**changes), _Containers)

    @pytest.mark.parametrize(
        ("changes", "reason"),
        [
            pytest.param({"height": None}, "有限な数値または「auto」が必要です: None"),
            pytest.param({"version": True}, "「1」が必要です: True"),
            pytest.param({"note": 1}, "文字列または未設定（null）が必要です: 1"),
        ],
    )
    def test_reasons_name_the_expected_values_in_words(
        self, changes: dict[str, object], reason: str
    ):
        """理由文は運転者が読むので、Python の型表記ではなく呼び名と候補の値を挙げる."""
        data = {"height": "auto", "version": 1, "note": None, **changes}

        with pytest.raises(ValueError) as raised:
            structure_document(data, _Choices)
        assert str(raised.value) == reason

    def test_a_union_takes_the_first_member_that_accepts_the_value(self):
        restored = structure_document(
            {"int_first": 1, "float_first": 1}, _Unions, int_as_float=True
        )

        assert type(restored.int_first) is int
        assert type(restored.float_first) is float


class TestParseDocument:
    def test_restores_a_document_of_the_kind_and_version(self):
        assert _parse(_document()) == (_Document("sample", 2, 1.5), None)

    @pytest.mark.parametrize("version", [1, 3, "2", True, None])
    def test_rejects_other_versions_without_migrating(self, version: object):
        document, error = _parse(_document(schema_version=version))

        assert document is None
        assert error == f"未対応の見本です: schema_version={version!r}"

    def test_checks_the_version_before_the_kind(self):
        _, error = _parse(_document(kind="other", schema_version=1))

        assert error == "未対応の見本です: schema_version=1"

    def test_rejects_another_kind(self):
        _, error = _parse(_document(kind="other"))

        assert error == "見本ではありません: kind='other'"

    def test_reports_a_malformed_body_with_the_label(self):
        _, error = _parse(_document(extra=1))

        assert error == "見本 schema v2 が不正です: 未知のキーです: extra"
