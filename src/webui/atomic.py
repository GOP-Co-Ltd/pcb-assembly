"""ファイルの atomic 書き込みユーティリティ."""

from __future__ import annotations

import tempfile
from pathlib import Path


def write_text_atomic(path: Path, text: str) -> None:
    """同一ディレクトリの一時ファイル経由で atomic にテキストを書き込む.

    書き込み中に他プロセス/スレッドが読んでも torn read（部分・空の内容）は
    発生しない。失敗時は一時ファイルを削除し、``path`` は変更しない。

    Args:
        path: 書き込み先（親ディレクトリは存在している必要がある）
        text: 書き込む内容（UTF-8）
    """
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            encoding="utf-8",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(text)
        tmp_path.replace(path)
    except Exception:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
        raise
