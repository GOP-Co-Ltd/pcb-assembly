"""テスト用の machine.toml を機能の節で書き換える."""

from __future__ import annotations

import tomlkit


def pnp_only(text: str) -> str:
    """``[solder]`` を外して空の ``[pnp]`` を足した内容（部品実装だけの機体）."""
    document = tomlkit.parse(text)
    del document["solder"]
    document.add("pnp", tomlkit.table())
    return tomlkit.dumps(document)
