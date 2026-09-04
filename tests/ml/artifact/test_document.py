"""Kind と schema_version を持つ成果物 document の公開契約."""

import json
from pathlib import Path

import attrs
import pytest

from ml.artifact.document import (
    DocumentKind,
    load_document,
    save_document,
    structure_document,
    unstructure_document,
)
from ml.serialization import make_strict_converter

SPLIT = DocumentKind(kind="ml-split-manifest", schema_version=1)


@attrs.frozen
class SplitManifest:
    seed: int
    train_group_ids: tuple[str, ...]
    validation_group_ids: tuple[str, ...]


def _manifest() -> SplitManifest:
    return SplitManifest(
        seed=7,
        train_group_ids=("session-a", "session-b"),
        validation_group_ids=("session-c",),
    )


class TestUnstructureDocument:
    """エンベロープを被せた素の dict."""

    def test_adds_kind_and_schema_version(self):
        payload = unstructure_document(
            _manifest(), kind=SPLIT, converter=make_strict_converter()
        )

        assert payload["kind"] == "ml-split-manifest"
        assert payload["schema_version"] == 1
        assert payload["seed"] == 7
        assert payload["train_group_ids"] == ["session-a", "session-b"]

    def test_rejects_a_payload_that_already_uses_envelope_keys(self):
        @attrs.frozen
        class Conflicting:
            kind: str

        with pytest.raises(ValueError, match="kind"):
            unstructure_document(
                Conflicting(kind="oops"), kind=SPLIT, converter=make_strict_converter()
            )


class TestStructureDocument:
    """エンベロープ検証と厳格な構造化."""

    def test_round_trips(self):
        converter = make_strict_converter()
        payload = unstructure_document(_manifest(), kind=SPLIT, converter=converter)

        value, error = structure_document(
            payload, SplitManifest, kind=SPLIT, converter=converter
        )

        assert error is None
        assert value == _manifest()

    def test_reports_a_mismatched_kind(self):
        converter = make_strict_converter()
        payload = unstructure_document(_manifest(), kind=SPLIT, converter=converter)
        payload["kind"] = "ml-other-document"

        value, error = structure_document(
            payload, SplitManifest, kind=SPLIT, converter=converter
        )

        assert value is None
        assert error is not None
        assert "ml-other-document" in error

    def test_reports_a_mismatched_schema_version(self):
        converter = make_strict_converter()
        payload = unstructure_document(_manifest(), kind=SPLIT, converter=converter)
        payload["schema_version"] = 2

        value, error = structure_document(
            payload, SplitManifest, kind=SPLIT, converter=converter
        )

        assert value is None
        assert error is not None
        assert "schema_version" in error

    def test_reports_a_missing_envelope(self):
        converter = make_strict_converter()

        value, error = structure_document(
            {"seed": 7, "train_group_ids": [], "validation_group_ids": []},
            SplitManifest,
            kind=SPLIT,
            converter=converter,
        )

        assert value is None
        assert error is not None
        assert "kind" in error

    def test_reports_an_unknown_field(self):
        converter = make_strict_converter()
        payload = unstructure_document(_manifest(), kind=SPLIT, converter=converter)
        payload["unexpected"] = 1

        value, error = structure_document(
            payload, SplitManifest, kind=SPLIT, converter=converter
        )

        assert value is None
        assert error is not None
        assert "unexpected" in error

    def test_reports_an_implicitly_convertible_value(self):
        converter = make_strict_converter()
        payload = unstructure_document(_manifest(), kind=SPLIT, converter=converter)
        payload["seed"] = 7.0

        value, error = structure_document(
            payload, SplitManifest, kind=SPLIT, converter=converter
        )

        assert value is None
        assert error is not None
        assert "seed" in error


class TestSaveAndLoadDocument:
    """ファイル経由の往復."""

    def test_saves_a_sorted_json_document(self, tmp_path: Path):
        target = tmp_path / "split.json"

        save_document(
            target, _manifest(), kind=SPLIT, converter=make_strict_converter()
        )

        text = target.read_text(encoding="utf-8")
        assert text.endswith("\n")
        assert json.loads(text)["kind"] == "ml-split-manifest"

    def test_loads_back_an_equal_value(self, tmp_path: Path):
        converter = make_strict_converter()
        target = tmp_path / "split.json"
        save_document(target, _manifest(), kind=SPLIT, converter=converter)

        value, error = load_document(
            target, SplitManifest, kind=SPLIT, converter=converter
        )

        assert error is None
        assert value == _manifest()

    def test_reports_a_missing_file(self, tmp_path: Path):
        value, error = load_document(
            tmp_path / "absent.json",
            SplitManifest,
            kind=SPLIT,
            converter=make_strict_converter(),
        )

        assert value is None
        assert error is not None
        assert "absent.json" in error

    def test_reports_malformed_json(self, tmp_path: Path):
        target = tmp_path / "split.json"
        target.write_text("{not json", encoding="utf-8")

        value, error = load_document(
            target, SplitManifest, kind=SPLIT, converter=make_strict_converter()
        )

        assert value is None
        assert error is not None

    def test_reports_a_json_document_that_is_not_an_object(self, tmp_path: Path):
        target = tmp_path / "split.json"
        target.write_text("[1, 2]", encoding="utf-8")

        value, error = load_document(
            target, SplitManifest, kind=SPLIT, converter=make_strict_converter()
        )

        assert value is None
        assert error is not None
