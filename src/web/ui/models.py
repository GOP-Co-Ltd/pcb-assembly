"""UI frontend 自身が返す JSON の pydantic モデル.

backend の値を中継するモデルは `web.api.models` を使う。ここに置くのは frontend が
持つ情報（マシン登録一覧）だけ。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class MachineSummary(BaseModel):
    """マシン選択 UI 1 行分（表示名も「現在のマシンか」もサーバが決める）."""

    machine_id: str
    label: str
    name: str | None
    host: str
    port: int
    machine_type: str | None
    source: Literal["static", "mdns"]
    current: bool


class MachinesResponse(BaseModel):
    """``GET /api/machines`` の応答."""

    machines: list[MachineSummary]
