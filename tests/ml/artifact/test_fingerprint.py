"""再現可能な成果物 fingerprint の公開契約."""

import hashlib
import json
from pathlib import Path

import pytest

from ml.artifact.fingerprint import (
    canonical_json,
    fingerprint_json,
    sha256_bytes,
    sha256_file,
)


class TestCanonicalJson:
    """同じ内容が同じ文字列になる正準表現."""

    def test_is_independent_of_key_order(self):
        first = canonical_json({"beta": 1, "alpha": [2, 3]})
        second = canonical_json({"alpha": [2, 3], "beta": 1})

        assert first == second

    def test_has_no_insignificant_whitespace(self):
        assert canonical_json({"alpha": 1, "beta": 2}) == '{"alpha":1,"beta":2}'

    def test_keeps_non_ascii_characters(self):
        assert canonical_json({"name": "塗布量"}) == '{"name":"塗布量"}'

    def test_preserves_list_order(self):
        assert canonical_json([3, 1, 2]) == "[3,1,2]"

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_rejects_non_finite_numbers(self, value: float):
        with pytest.raises(ValueError):
            canonical_json({"volume": value})


class TestSha256:
    """素の 16 進ダイジェスト."""

    def test_hashes_bytes(self):
        assert sha256_bytes(b"payload") == hashlib.sha256(b"payload").hexdigest()

    def test_hashes_file_content(self, tmp_path: Path):
        target = tmp_path / "artifact.bin"
        target.write_bytes(b"payload")

        assert sha256_file(target) == hashlib.sha256(b"payload").hexdigest()

    def test_digest_is_lowercase_hex_of_fixed_length(self):
        digest = sha256_bytes(b"")

        assert len(digest) == 64
        assert all(character in "0123456789abcdef" for character in digest)


class TestFingerprintJson:
    """接頭辞付きの内容 fingerprint."""

    def test_prefixes_the_digest(self):
        fingerprint = fingerprint_json({"alpha": 1})

        assert fingerprint.startswith("sha256:")
        assert fingerprint[len("sha256:") :] == sha256_bytes(
            canonical_json({"alpha": 1}).encode("utf-8")
        )

    def test_is_independent_of_key_order(self):
        assert fingerprint_json({"beta": 2, "alpha": 1}) == fingerprint_json(
            {"alpha": 1, "beta": 2}
        )

    def test_differs_when_content_differs(self):
        assert fingerprint_json({"alpha": 1}) != fingerprint_json({"alpha": 2})

    def test_matches_a_manually_canonicalized_document(self):
        document = {"kind": "ml-test", "schema_version": 1, "values": [1, 2]}
        expected = hashlib.sha256(
            json.dumps(
                document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()

        assert fingerprint_json(document) == f"sha256:{expected}"
