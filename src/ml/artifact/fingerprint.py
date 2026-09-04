"""内容から決まる再現可能な fingerprint.

同じ内容なら、書き出し順・辞書のキー順・マウント位置が違っても同じ値になることを 保証する。dataset や設定の同一性判定に使う。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

_READ_CHUNK_BYTES = 1024 * 1024
_FINGERPRINT_PREFIX = "sha256:"


def canonical_json(value: object) -> str:
    """キー順と空白を固定した JSON 表現を返す.

    ``NaN`` と無限大は JSON として交換できないため拒否する。
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_bytes(data: bytes) -> str:
    """素の 16 進 SHA-256 ダイジェストを返す."""

    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """ファイル内容の素の 16 進 SHA-256 ダイジェストを返す."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(_READ_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_json(value: object) -> str:
    """正準 JSON に対する ``sha256:`` 付きの内容 fingerprint を返す."""

    return _FINGERPRINT_PREFIX + sha256_bytes(canonical_json(value).encode("utf-8"))


__all__ = [
    "canonical_json",
    "fingerprint_json",
    "sha256_bytes",
    "sha256_file",
]
