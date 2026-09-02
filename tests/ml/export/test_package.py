from pathlib import Path

import pytest

from ml.export.package import publish_immutable_package


class TestPublishImmutablePackage:
    def test_rejects_duplicate_payload_file_names(self, tmp_path: Path):
        def write_payloads(_directory: Path) -> None:
            raise AssertionError("writer must not run for invalid payload names")

        with pytest.raises(ValueError) as exc_info:
            publish_immutable_package(
                tmp_path / "package",
                payload_files=("model.onnx", "model.onnx"),
                write_payloads=write_payloads,
            )

        assert "unique" in str(exc_info.value)

    @pytest.mark.parametrize("checksum_file", ("", "../SHA256SUMS", "nested/sums"))
    def test_rejects_non_plain_checksum_file(self, tmp_path: Path, checksum_file: str):
        def write_payloads(_directory: Path) -> None:
            raise AssertionError("writer must not run for an invalid checksum file")

        with pytest.raises(ValueError) as exc_info:
            publish_immutable_package(
                tmp_path / "package",
                payload_files=("model.onnx",),
                write_payloads=write_payloads,
                checksum_file=checksum_file,
            )

        assert "checksum" in str(exc_info.value).lower()
