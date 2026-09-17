"""`pcbasm.atomic` のテキスト・バイナリ書き込み契約（unit）.

同一ディレクトリの一時ファイル経由で置き換える契約:

- 置換後に内容が一致する
- 同一ディレクトリに一時ファイルを残さない
- 書き込み失敗時に既存ファイルを壊さない
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pcbasm.atomic import write_bytes_atomic, write_text_atomic


class TestWriteTextAtomic:
    """一時ファイル経由の atomic replace."""

    def test_creates_file_with_content(self, tmp_path: Path):
        path = tmp_path / "state.json"

        write_text_atomic(path, '{"a": 1}')

        assert path.read_text(encoding="utf-8") == '{"a": 1}'

    def test_replaces_existing_content(self, tmp_path: Path):
        path = tmp_path / "state.json"
        path.write_text("old", encoding="utf-8")

        write_text_atomic(path, "new")

        assert path.read_text(encoding="utf-8") == "new"

    def test_leaves_no_temporary_file(self, tmp_path: Path):
        path = tmp_path / "state.json"

        write_text_atomic(path, "content")

        assert [entry.name for entry in tmp_path.iterdir()] == ["state.json"]

    def test_failed_write_keeps_existing_file_and_removes_temporary(
        self, tmp_path: Path
    ):
        path = tmp_path / "state.json"
        path.write_text("intact", encoding="utf-8")

        # 単独サロゲートは UTF-8 へエンコードできない
        with pytest.raises(UnicodeEncodeError):
            write_text_atomic(path, "broken \ud800")

        assert path.read_text(encoding="utf-8") == "intact"
        assert [entry.name for entry in tmp_path.iterdir()] == ["state.json"]

    def test_missing_parent_directory_raises(self, tmp_path: Path):
        with pytest.raises(OSError):
            write_text_atomic(tmp_path / "missing" / "state.json", "content")


class TestWriteBytesAtomic:
    @pytest.mark.parametrize("existing", [False, True], ids=["new", "replace"])
    def test_preserves_exact_bytes_and_leaves_no_temporary_file(
        self, tmp_path: Path, existing: bool
    ):
        path = tmp_path / "board.bin"
        if existing:
            path.write_bytes(b"original")
        payload = b"\x00\xff\r\n\x80"

        write_bytes_atomic(path, payload)

        assert path.read_bytes() == payload
        assert list(tmp_path.iterdir()) == [path]

    def test_replaces_a_symlink_without_changing_its_target(self, tmp_path: Path):
        original = tmp_path / "original.bin"
        original.write_bytes(b"keep")
        link = tmp_path / "link.bin"
        link.symlink_to(original)

        write_bytes_atomic(link, b"replacement")

        assert original.read_bytes() == b"keep"
        assert not link.is_symlink()
        assert link.read_bytes() == b"replacement"
