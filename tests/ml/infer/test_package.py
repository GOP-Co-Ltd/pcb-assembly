from pathlib import Path

import pytest

from ml.infer.package import verify_immutable_package


class TestVerifyImmutablePackage:
    def test_rejects_duplicate_payload_file_names(self, tmp_path: Path):
        with pytest.raises(ValueError) as exc_info:
            verify_immutable_package(
                tmp_path / "package",
                payload_files=("model.onnx", "model.onnx"),
            )

        assert "unique" in str(exc_info.value)

    @pytest.mark.parametrize("checksum_file", ("", "../SHA256SUMS", "nested/sums"))
    def test_rejects_non_plain_checksum_file(self, tmp_path: Path, checksum_file: str):
        with pytest.raises(ValueError) as exc_info:
            verify_immutable_package(
                tmp_path / "package",
                payload_files=("model.onnx",),
                checksum_file=checksum_file,
            )

        assert "checksum" in str(exc_info.value).lower()
