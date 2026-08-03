"""クライアントの自己申告によるセッション同定（認証ではない）.

ブラウザは任意のリクエストへ独自ヘッダを付けられないため、frontend（`web.ui`）が
HTML ページレスポンスで cookie を発行し、proxy がヘッダ組み立ての 1 箇所で backend
へ載せ替える。ヘッダが無い経路（cookie が直接乗る WS ハンドシェイクや `img.src`）
では cookie を読む。

**これは認証ではなく自己申告である。** LAN 上の誰でも他人の `session_id` や表示名を
騙れる。操作権リースは「複数人が同時に装置へ指示を出す事故」を防ぐためのもので、
権限分離ではない。認証が無い現状より悪化しないので受容する。
"""

from __future__ import annotations

from urllib.parse import unquote

import attrs
from starlette.requests import HTTPConnection

from web.api.control import ClientIdentity

# ヘッダは proxy が注入する（表示名は latin-1 制約を避けるため quote 済みで届く）
SESSION_HEADER = "X-Pcbasm-Session"
NAME_HEADER = "X-Pcbasm-Client-Name"

# cookie は frontend が HTML ページレスポンスで発行する
SESSION_COOKIE = "pcbasm_session"
NAME_COOKIE = "pcbasm_name"

# セッションを名乗らないクライアント（curl や cookie 無効なブラウザ）の共有セッション
ANONYMOUS_SESSION = "anonymous"


def get_identity(connection: HTTPConnection) -> ClientIdentity:
    """リクエストからクライアント同定情報を解決する（HTTP / WebSocket 共通）.

    ``session_id`` はヘッダ → cookie → ``anonymous``、``display_name`` はヘッダ →
    cookie → ``名前未設定 (<key>)`` の順で解決する。壊れた入力（復元できない
    percent-encoding や非 ASCII の生バイト）は例外にせず次の候補へ落とす。

    Args:
        connection: HTTP リクエストまたは WebSocket 接続

    Returns:
        解決した同定情報
    """
    session_id = (
        _header_or_cookie(connection, SESSION_HEADER, SESSION_COOKIE)
        or ANONYMOUS_SESSION
    )
    identity = ClientIdentity(session_id=session_id, display_name=_name(connection))
    if not identity.display_name:
        identity = attrs.evolve(identity, display_name=f"名前未設定 ({identity.key})")
    return identity


def _header_or_cookie(
    connection: HTTPConnection, header: str, cookie: str
) -> str | None:
    """ヘッダ → cookie の順に非空の値を探す（無ければ None）."""
    for raw in (connection.headers.get(header), connection.cookies.get(cookie)):
        if raw is not None and (value := raw.strip()):
            return value
    return None


def _name(connection: HTTPConnection) -> str:
    """表示名をヘッダ → cookie の順に復元する（復元できなければ空文字）."""
    for raw in (
        connection.headers.get(NAME_HEADER),
        connection.cookies.get(NAME_COOKIE),
    ):
        if (name := _decode_name(raw)) is not None:
            return name
    return ""


def _decode_name(raw: str | None) -> str | None:
    """Percent-encoded な表示名を復元する（復元できない値は None）.

    HTTP/1.1 のヘッダ値は latin-1 なので、日本語の表示名は `urllib.parse.quote`
    済みで届く。非 ASCII の生バイトは latin-1 として復号された文字化けであり、
    表示名として採用しない（そのまま出すと全クライアントに文字化けが配られる）。
    """
    if raw is None or not raw.isascii():
        return None
    try:
        decoded = unquote(raw, errors="strict")
    except UnicodeDecodeError:
        return None
    return decoded.strip() or None
