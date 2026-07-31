"""`web.api.atomic.write_text_atomic` の仕様テスト（unit）.

計画書 MR1「新規 src/webui/atomic.py」節が契約:

- 置換後に内容が一致する
- 同一ディレクトリに一時ファイルを残さない
- 書き込み失敗時に既存ファイルを壊さない
"""

from __future__ import annotations

from pathlib import Path

import pytest

from web.api.atomic import write_text_atomic


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

    def test_roundtrips_non_ascii(self, tmp_path: Path):
        path = tmp_path / "state.json"

        write_text_atomic(path, "日本語のテキスト")

        assert path.read_text(encoding="utf-8") == "日本語のテキスト"

    def test_failed_write_keeps_existing_file_and_removes_temporary(
        self, tmp_path: Path
    ):
        path = tmp_path / "state.json"
        path.write_text("intact", encoding="utf-8")

        # 単独サロゲートは UTF-8 へエンコードできず、一時ファイル生成後に失敗する
        with pytest.raises(UnicodeEncodeError):
            write_text_atomic(path, "broken \ud800")

        assert path.read_text(encoding="utf-8") == "intact"
        assert [entry.name for entry in tmp_path.iterdir()] == ["state.json"]

    def test_missing_parent_directory_raises(self, tmp_path: Path):
        with pytest.raises(OSError):
            write_text_atomic(tmp_path / "missing" / "state.json", "content")
