"""クライアントの自己申告によるセッション同定で、frontend と backend が共有する名前.

frontend が HTML ページの応答で cookie を発行し、proxy がそれをヘッダへ載せ替えて backend へ渡す。
"""

from __future__ import annotations

# proxy が注入する（表示名は latin-1 制約を避けるため quote 済みで届く）
SESSION_HEADER = "X-Pcbasm-Session"
NAME_HEADER = "X-Pcbasm-Client-Name"

SESSION_COOKIE = "pcbasm_session"
NAME_COOKIE = "pcbasm_name"
