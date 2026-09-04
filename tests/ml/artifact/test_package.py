"""不変パッケージの公開と、active pointer 切替・rollback の公開契約."""

from pathlib import Path

import pytest

from ml.artifact.fingerprint import sha256_bytes
from ml.artifact.package import (
    CHECKSUM_FILENAME,
    ActivePointer,
    ImmutablePackage,
)

PAYLOAD = {"model.onnx": b"onnx-bytes", "manifest.json": b'{"kind":"ml-test"}\n'}


def _write_payload(directory: Path) -> None:
    for filename, data in PAYLOAD.items():
        (directory / filename).write_bytes(data)


def _publish(destination: Path) -> ImmutablePackage:
    return ImmutablePackage.publish(
        destination,
        payload_filenames=PAYLOAD,
        write_payloads=_write_payload,
    )


def _visible_names(directory: Path) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir())


class TestImmutablePackagePublish:
    """完成後に 1 度だけ rename する公開手順."""

    def test_publishes_payload_and_checksum_file(self, tmp_path: Path):
        package = _publish(tmp_path / "model-v1")

        assert package.path == (tmp_path / "model-v1").resolve()
        assert _visible_names(package.path) == [
            CHECKSUM_FILENAME,
            "manifest.json",
            "model.onnx",
        ]
        assert package.checksums["model.onnx"] == sha256_bytes(b"onnx-bytes")

    def test_checksum_file_lists_every_payload(self, tmp_path: Path):
        package = _publish(tmp_path / "model-v1")

        lines = (
            (package.path / CHECKSUM_FILENAME).read_text(encoding="utf-8").splitlines()
        )

        assert lines == [
            f"{sha256_bytes(PAYLOAD['manifest.json'])}  manifest.json",
            f"{sha256_bytes(PAYLOAD['model.onnx'])}  model.onnx",
        ]

    def test_refuses_to_overwrite_an_existing_package(self, tmp_path: Path):
        destination = tmp_path / "model-v1"
        _publish(destination)

        with pytest.raises(FileExistsError, match="model-v1"):
            _publish(destination)

    def test_writer_failure_leaves_nothing_behind(self, tmp_path: Path):
        def fail(directory: Path) -> None:
            (directory / "model.onnx").write_bytes(b"partial")
            raise RuntimeError("payload 生成に失敗")

        with pytest.raises(RuntimeError, match="payload 生成に失敗"):
            ImmutablePackage.publish(
                tmp_path / "model-v1",
                payload_filenames=PAYLOAD,
                write_payloads=fail,
            )

        assert _visible_names(tmp_path) == []

    def test_rejects_a_writer_that_omits_a_declared_file(self, tmp_path: Path):
        with pytest.raises(ValueError, match="manifest.json"):
            ImmutablePackage.publish(
                tmp_path / "model-v1",
                payload_filenames=PAYLOAD,
                write_payloads=lambda directory: (directory / "model.onnx").write_bytes(
                    b"onnx-bytes"
                ),
            )

        assert _visible_names(tmp_path) == []

    def test_rejects_a_writer_that_adds_an_undeclared_file(self, tmp_path: Path):
        def write_extra(directory: Path) -> None:
            _write_payload(directory)
            (directory / "notes.txt").write_bytes(b"extra")

        with pytest.raises(ValueError, match="notes.txt"):
            ImmutablePackage.publish(
                tmp_path / "model-v1",
                payload_filenames=PAYLOAD,
                write_payloads=write_extra,
            )

        assert _visible_names(tmp_path) == []

    def test_rejects_a_symlink_payload(self, tmp_path: Path):
        outside = tmp_path / "outside.bin"
        outside.write_bytes(b"outside")

        def write_symlink(directory: Path) -> None:
            (directory / "manifest.json").write_bytes(PAYLOAD["manifest.json"])
            (directory / "model.onnx").symlink_to(outside)

        with pytest.raises(ValueError, match="model.onnx"):
            ImmutablePackage.publish(
                tmp_path / "model-v1",
                payload_filenames=PAYLOAD,
                write_payloads=write_symlink,
            )

    @pytest.mark.parametrize(
        "filename", ["", "nested/model.onnx", "..", CHECKSUM_FILENAME]
    )
    def test_rejects_unusable_payload_names(self, tmp_path: Path, filename: str):
        with pytest.raises(ValueError):
            ImmutablePackage.publish(
                tmp_path / "model-v1",
                payload_filenames=[filename],
                write_payloads=_write_payload,
            )


class TestImmutablePackageVerify:
    """公開済みパッケージの改竄検出."""

    def test_accepts_an_untouched_package(self, tmp_path: Path):
        published = _publish(tmp_path / "model-v1")

        verified, error = ImmutablePackage.verify(published.path)

        assert error is None
        assert verified is not None
        assert verified.checksums == published.checksums

    def test_detects_modified_content(self, tmp_path: Path):
        published = _publish(tmp_path / "model-v1")
        (published.path / "model.onnx").write_bytes(b"tampered")

        verified, error = ImmutablePackage.verify(published.path)

        assert verified is None
        assert error is not None
        assert "model.onnx" in error

    def test_detects_a_removed_file(self, tmp_path: Path):
        published = _publish(tmp_path / "model-v1")
        (published.path / "manifest.json").unlink()

        verified, error = ImmutablePackage.verify(published.path)

        assert verified is None
        assert error is not None
        assert "manifest.json" in error

    def test_detects_an_added_file(self, tmp_path: Path):
        published = _publish(tmp_path / "model-v1")
        (published.path / "notes.txt").write_bytes(b"extra")

        verified, error = ImmutablePackage.verify(published.path)

        assert verified is None
        assert error is not None
        assert "notes.txt" in error

    def test_reports_a_missing_checksum_file(self, tmp_path: Path):
        published = _publish(tmp_path / "model-v1")
        (published.path / CHECKSUM_FILENAME).unlink()

        verified, error = ImmutablePackage.verify(published.path)

        assert verified is None
        assert error is not None
        assert CHECKSUM_FILENAME in error

    def test_detects_a_payload_replaced_by_a_directory(self, tmp_path: Path):
        published = _publish(tmp_path / "model-v1")
        (published.path / "model.onnx").unlink()
        (published.path / "model.onnx").mkdir()

        verified, error = ImmutablePackage.verify(published.path)

        assert verified is None
        assert error is not None
        assert "model.onnx" in error

    def test_reports_a_malformed_checksum_line(self, tmp_path: Path):
        published = _publish(tmp_path / "model-v1")
        (published.path / CHECKSUM_FILENAME).write_text(
            "not-a-digest  model.onnx\n", encoding="utf-8"
        )

        verified, error = ImmutablePackage.verify(published.path)

        assert verified is None
        assert error is not None
        assert CHECKSUM_FILENAME in error

    def test_reports_a_missing_package_directory(self, tmp_path: Path):
        verified, error = ImmutablePackage.verify(tmp_path / "absent")

        assert verified is None
        assert error is not None
        assert "absent" in error

    def test_reports_an_unexpected_file_set(self, tmp_path: Path):
        published = _publish(tmp_path / "model-v1")

        verified, error = ImmutablePackage.verify(
            published.path, expected_filenames=["model.onnx", "preprocess.json"]
        )

        assert verified is None
        assert error is not None
        assert "preprocess.json" in error


class TestActivePointer:
    """Active パッケージの atomic 切替と 1 世代 rollback."""

    def test_first_switch_has_no_predecessor(self, tmp_path: Path):
        package = _publish(tmp_path / "model-v1")
        pointer_file = tmp_path / "active-model.json"

        active, error = ActivePointer.switch(pointer_file, package.path)

        assert error is None
        assert active is not None
        assert active.active_package_path == package.path
        assert active.previous_package_path is None

    def test_second_switch_retains_the_predecessor(self, tmp_path: Path):
        first = _publish(tmp_path / "model-v1")
        second = _publish(tmp_path / "model-v2")
        pointer_file = tmp_path / "active-model.json"
        ActivePointer.switch(pointer_file, first.path)

        active, error = ActivePointer.switch(pointer_file, second.path)

        assert error is None
        assert active is not None
        assert active.active_package_path == second.path
        assert active.previous_package_path == first.path

    def test_switching_to_the_same_package_keeps_the_predecessor(self, tmp_path: Path):
        first = _publish(tmp_path / "model-v1")
        second = _publish(tmp_path / "model-v2")
        pointer_file = tmp_path / "active-model.json"
        ActivePointer.switch(pointer_file, first.path)
        ActivePointer.switch(pointer_file, second.path)

        active, error = ActivePointer.switch(pointer_file, second.path)

        assert error is None
        assert active is not None
        assert active.previous_package_path == first.path

    def test_refuses_to_activate_a_tampered_package(self, tmp_path: Path):
        package = _publish(tmp_path / "model-v1")
        (package.path / "model.onnx").write_bytes(b"tampered")
        pointer_file = tmp_path / "active-model.json"

        active, error = ActivePointer.switch(pointer_file, package.path)

        assert active is None
        assert error is not None
        assert not pointer_file.exists()

    def test_refuses_a_pointer_inside_the_package(self, tmp_path: Path):
        package = _publish(tmp_path / "model-v1")

        with pytest.raises(ValueError):
            ActivePointer.switch(package.path / "active.json", package.path)

    def test_loads_back_the_recorded_pointer(self, tmp_path: Path):
        package = _publish(tmp_path / "model-v1")
        pointer_file = tmp_path / "active-model.json"
        written, _ = ActivePointer.switch(pointer_file, package.path)

        loaded, error = ActivePointer.load(pointer_file)

        assert error is None
        assert loaded == written

    def test_reports_a_missing_pointer(self, tmp_path: Path):
        loaded, error = ActivePointer.load(tmp_path / "active-model.json")

        assert loaded is None
        assert error is not None
        assert "active-model.json" in error

    def test_rollback_swaps_active_and_previous(self, tmp_path: Path):
        first = _publish(tmp_path / "model-v1")
        second = _publish(tmp_path / "model-v2")
        pointer_file = tmp_path / "active-model.json"
        ActivePointer.switch(pointer_file, first.path)
        ActivePointer.switch(pointer_file, second.path)

        rolled_back, error = ActivePointer.rollback(pointer_file)

        assert error is None
        assert rolled_back is not None
        assert rolled_back.active_package_path == first.path
        assert rolled_back.previous_package_path == second.path

    def test_rollback_reports_when_there_is_no_predecessor(self, tmp_path: Path):
        package = _publish(tmp_path / "model-v1")
        pointer_file = tmp_path / "active-model.json"
        ActivePointer.switch(pointer_file, package.path)

        rolled_back, error = ActivePointer.rollback(pointer_file)

        assert rolled_back is None
        assert error is not None
