"""成果物ファイルを部分書き込み状態で見せない atomic 書き込み.

``ml`` が生成する manifest / report / checkpoint はすべてこの入口を通す。
一時ファイルへ書いて ``fsync`` し、必要なら読み戻し検証を挟んでから
``os.replace`` で公開する。中断されても、公開済みのファイルは常に
「直前の完全な内容」か「今回の完全な内容」のどちらかになる。
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

# 戻り値は使わない。``lambda stream: stream.write(data)`` を書けるよう object にする。
type StreamWriter = Callable[[BinaryIO], object]
type ReadbackValidator = Callable[[Path], object]


def atomic_write_stream(
    path: Path,
    write: StreamWriter,
    *,
    validate_readback: ReadbackValidator | None = None,
) -> None:
    """``write`` が書いた内容を、完成後に 1 度だけ ``path`` へ公開する.

    ``validate_readback`` を渡すと、公開前に一時ファイルを読み戻して検証できる。
    検証が例外を送出した場合は公開せず、既存のファイルをそのまま残す。
    """

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # 同一ディレクトリへ作る。別ファイルシステムだと os.replace が atomic にならない。
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        if validate_readback is not None:
            validate_readback(temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    fsync_directory(destination.parent)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """バイト列を atomic に書き込む."""

    def write(stream: BinaryIO) -> None:
        stream.write(data)

    atomic_write_stream(path, write)


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """テキストを atomic に書き込む."""

    atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(path: Path, value: object) -> None:
    """整形済み JSON を atomic に書き込む.

    キーを整列して差分を読みやすくし、``NaN`` と無限大はワイヤへ出す前に拒否する。
    """

    text = json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
    )
    atomic_write_text(path, f"{text}\n")


def fsync_directory(directory: Path) -> None:
    """ディレクトリ entry の変更（rename や作成）自体を永続化する."""

    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "ReadbackValidator",
    "StreamWriter",
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_write_stream",
    "atomic_write_text",
    "fsync_directory",
]
