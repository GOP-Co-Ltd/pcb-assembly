"""UI frontend 自身が返す JSON の pydantic モデル.

backend の値を中継するモデルは `web.api.models` を使う。ここに置くのは frontend が
持つ情報（マシン登録一覧）だけ。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# 更新ページの入口（機体ごとのページはここからリンクする）
UPDATE_INDEX_PATH = "/update"


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


class UpdateNoticeResponse(BaseModel):
    """``GET /api/update-notice`` の応答（トップバーの更新通知バッジ 1 個分）.

    見出し・詳細・遷移先はすべてサーバが組む。JS は ``available`` で出し入れして
    残りを DOM に流すだけにする（`webui-thin-wrapper`）。
    """

    # 更新が 1 つでも待っているか（バッジを出すかどうか）
    available: bool = False
    # バッジの表示文字列（出さないときは空）
    label: str = ""
    # tooltip に出す内訳（ホストごとに 1 行）
    detail: str = ""
    # バッジの遷移先（複数ホストが待っているときは UI サーバーの更新ページ）
    href: str = UPDATE_INDEX_PATH
