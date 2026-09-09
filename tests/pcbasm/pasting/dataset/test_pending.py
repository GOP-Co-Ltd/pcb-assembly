"""計量質量が未確定の dataset metadata（pending schema v1）の公開契約.

``pending.json`` は「metadata v2 から計量質量に依存する値だけを抜いた doc」で、
その値とは ``total`` と、sample / purge の ``measured_volume_ul`` の 3 つ。
blank の ``measured_volume_ul`` は常に 0.0 なので質量に依存しない。

on-disk 契約の出典は ``data/testing/schemas/paste_dataset_pending_v1.json``
（同じ収集の完成形が ``paste_dataset_metadata_v2.json``）。この 2 ファイルを
``finalize_pending`` が計量質量 1 つで結ぶことを検証する。
"""

import json
from pathlib import Path
from typing import Any

import pytest

from pcbasm.pasting.dataset.metadata import PasteDatasetMetadata, parse_metadata
from pcbasm.pasting.dataset.pending import (
    PENDING_KIND,
    PENDING_SCHEMA_VERSION,
    PasteDatasetPending,
    finalize_pending,
    parse_pending,
)
from tests.helpers import TESTING_DATA_DIR

METADATA_V2 = TESTING_DATA_DIR / "schemas" / "paste_dataset_metadata_v2.json"
PENDING_V1 = TESTING_DATA_DIR / "schemas" / "paste_dataset_pending_v1.json"
# PENDING_V1 と METADATA_V2 は同じ収集で、質量 0.945 mg / 密度 3.78 が対応する
MEASURED_MASS_MG = 0.945


def _document(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _pending(payload: dict[str, Any] | None = None) -> PasteDatasetPending:
    pending, error = parse_pending(
        _document(PENDING_V1) if payload is None else payload
    )

    assert error is None, error
    assert pending is not None
    return pending


def _metadata() -> PasteDatasetMetadata:
    metadata, error = parse_metadata(_document(METADATA_V2))

    assert error is None, error
    assert metadata is not None
    return metadata


class TestParsePending:
    """Pending schema v1 の strict な復元."""

    def test_restores_the_on_disk_document(self):
        pending = _pending()

        assert pending.kind == PENDING_KIND
        assert pending.schema_version == PENDING_SCHEMA_VERSION
        assert len(pending.samples) == 1
        assert len(pending.blanks) == 1

    def test_round_trips_through_to_dict(self):
        document = json.loads(json.dumps(_pending().to_dict()))

        assert document == _document(PENDING_V1)

    def test_rejects_unknown_schema_version(self):
        payload = _document(PENDING_V1) | {"schema_version": 2}

        pending, error = parse_pending(payload)

        assert pending is None
        assert error is not None
        assert "schema_version" in error

    @pytest.mark.parametrize("key", ["total", "unexpected"])
    def test_rejects_extra_key(self, key: str):
        payload = _document(PENDING_V1) | {key: 1.0}

        pending, error = parse_pending(payload)

        assert pending is None
        assert error is not None

    def test_rejects_sample_that_still_carries_a_measured_volume(self):
        payload = _document(PENDING_V1)
        payload["samples"][0]["measured_volume_ul"] = 0.15

        pending, error = parse_pending(payload)

        assert pending is None
        assert error is not None


class TestFinalizePending:
    """計量質量 1 つで metadata v2 を確定する."""

    def test_produces_the_completed_metadata_document(self):
        metadata, error = finalize_pending(
            _pending(), measured_mass_mg=MEASURED_MASS_MG
        )

        assert error is None, error
        assert metadata is not None
        assert metadata == _metadata()

    def test_allocates_the_measured_volume_by_rotation_ratio(self):
        metadata, error = finalize_pending(
            _pending(), measured_mass_mg=MEASURED_MASS_MG * 2.0
        )

        assert error is None, error
        assert metadata is not None
        assert metadata.total.measured_mass_mg == pytest.approx(MEASURED_MASS_MG * 2.0)
        assert metadata.total.measured_volume_ul == pytest.approx(0.5)
        assert metadata.purge.measured_volume_ul == pytest.approx(0.2)
        assert metadata.samples[0].measured_volume_ul == pytest.approx(0.3)
        assert metadata.blanks[0].measured_volume_ul == 0.0

    def test_keeps_the_shuffle_seed_that_produced_the_layout(self):
        metadata, error = finalize_pending(
            _pending(), measured_mass_mg=MEASURED_MASS_MG
        )

        assert error is None, error
        assert metadata is not None
        assert metadata.config.shuffle_seed == _pending().config.shuffle_seed

    @pytest.mark.parametrize("mass", [0.0, -1.0, float("nan"), float("inf")])
    def test_rejects_non_positive_mass(self, mass: float):
        metadata, error = finalize_pending(_pending(), measured_mass_mg=mass)

        assert metadata is None
        assert error is not None
        assert "質量" in error

    def test_rejects_a_document_without_any_dispense(self):
        payload = _document(PENDING_V1)
        payload["samples"] = []
        payload["purge"]["execution"]["rotations"] = 0.0

        metadata, error = finalize_pending(
            _pending(payload), measured_mass_mg=MEASURED_MASS_MG
        )

        assert metadata is None
        assert error is not None
