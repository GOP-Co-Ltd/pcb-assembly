"""成果物ファイルを部分書き込み状態で見せない atomic 書き込みの公開契約."""

import json
from pathlib import Path

import pytest

from ml.artifact.atomic import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_stream,
    atomic_write_text,
)


def _visible_names(directory: Path) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir())


class TestAtomicWriteStream:
    """書き込み callback を使う低水準 API の公開契約."""

    def test_creates_parent_directories_and_publishes_content(self, tmp_path: Path):
        target = tmp_path / "nested" / "deeper" / "artifact.bin"

        atomic_write_stream(target, lambda stream: stream.write(b"payload"))

        assert target.read_bytes() == b"payload"

    def test_replaces_existing_file(self, tmp_path: Path):
        target = tmp_path / "artifact.bin"
        target.write_bytes(b"old")

        atomic_write_stream(target, lambda stream: stream.write(b"new"))

        assert target.read_bytes() == b"new"
        assert _visible_names(tmp_path) == ["artifact.bin"]

    def test_writer_failure_leaves_no_temporary_and_keeps_previous_content(
        self, tmp_path: Path
    ):
        target = tmp_path / "artifact.bin"
        target.write_bytes(b"old")

        def fail(stream):
            stream.write(b"partial")
            raise RuntimeError("書き込み失敗")

        with pytest.raises(RuntimeError, match="書き込み失敗"):
            atomic_write_stream(target, fail)

        assert target.read_bytes() == b"old"
        assert _visible_names(tmp_path) == ["artifact.bin"]

    def test_writer_failure_does_not_create_target(self, tmp_path: Path):
        target = tmp_path / "artifact.bin"

        def fail(stream):
            raise RuntimeError("書き込み失敗")

        with pytest.raises(RuntimeError, match="書き込み失敗"):
            atomic_write_stream(target, fail)

        assert not target.exists()
        assert _visible_names(tmp_path) == []

    def test_readback_validation_failure_does_not_publish(self, tmp_path: Path):
        target = tmp_path / "artifact.bin"
        target.write_bytes(b"old")

        def reject(path: Path) -> None:
            raise ValueError(f"読み戻し検証に失敗しました: {path.name}")

        with pytest.raises(ValueError, match="読み戻し検証に失敗しました"):
            atomic_write_stream(
                target,
                lambda stream: stream.write(b"new"),
                validate_readback=reject,
            )

        assert target.read_bytes() == b"old"
        assert _visible_names(tmp_path) == ["artifact.bin"]

    def test_readback_validation_sees_the_written_content(self, tmp_path: Path):
        target = tmp_path / "artifact.bin"
        observed: list[bytes] = []

        atomic_write_stream(
            target,
            lambda stream: stream.write(b"payload"),
            validate_readback=lambda path: observed.append(path.read_bytes()),
        )

        assert observed == [b"payload"]


class TestAtomicWriteBytesAndText:
    """Bytes / text の薄い入口."""

    def test_writes_bytes(self, tmp_path: Path):
        target = tmp_path / "artifact.bin"

        atomic_write_bytes(target, b"\x00\x01\x02")

        assert target.read_bytes() == b"\x00\x01\x02"

    def test_writes_utf8_text_without_ascii_escaping(self, tmp_path: Path):
        target = tmp_path / "artifact.txt"

        atomic_write_text(target, "塗布量\n")

        assert target.read_text(encoding="utf-8") == "塗布量\n"


class TestAtomicWriteJson:
    """成果物 JSON の整形と拒否条件."""

    def test_sorts_keys_and_ends_with_newline(self, tmp_path: Path):
        target = tmp_path / "artifact.json"

        atomic_write_json(target, {"beta": 1, "alpha": 2})

        text = target.read_text(encoding="utf-8")
        assert text.endswith("\n")
        assert text.index('"alpha"') < text.index('"beta"')
        assert json.loads(text) == {"alpha": 2, "beta": 1}

    def test_keeps_non_ascii_readable(self, tmp_path: Path):
        target = tmp_path / "artifact.json"

        atomic_write_json(target, {"name": "塗布量"})

        assert "塗布量" in target.read_text(encoding="utf-8")

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_rejects_non_finite_numbers(self, tmp_path: Path, value: float):
        target = tmp_path / "artifact.json"

        with pytest.raises(ValueError):
            atomic_write_json(target, {"volume": value})

        assert not target.exists()
        assert _visible_names(tmp_path) == []
