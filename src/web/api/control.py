"""操作権リース（複数ユーザー同時利用時の「操作は 1 セッションのみ」）.

閲覧は自由で、変更系の操作は操作権を持つ 1 セッションだけに許す。

緊急停止と abort は安全のため誰でも常に可能なので、ここではゲートしない。

FastAPI に依存しない純ロジックである。

自動解放はしない。時間・無操作・WebSocket の切断では失効しない。
保持者が替わるのは、空きのときの ``claim`` と、``takeover`` による奪取だけである。
空きに戻るのは、保持者自身の ``release`` とプロセス再起動の 2 つだけである。
リースはメモリ上にしか無いので、再起動（更新・ファームウェア再起動を含む）で消える。
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable

import attrs

_KEY_LENGTH = 8


@attrs.frozen
class ClientIdentity:
    """操作権を要求するクライアントの同定情報.

    ``session_id`` は自己申告（LAN 上の誰でも騙れる）であり認証ではない。
    生の ``session_id`` は API・UI・ログに出さず、公開表現には ``key`` を使う。

    Attributes:
        session_id: セッション識別子（外部に出さない）
        display_name: 画面に出す表示名
    """

    session_id: str
    display_name: str

    @property
    def key(self) -> str:
        """``session_id`` の公開表現（sha256 の先頭 8 hex）."""
        digest = hashlib.sha256(self.session_id.encode()).hexdigest()
        return digest[:_KEY_LENGTH]


@attrs.frozen
class LeaseInfo:
    """操作権リースの公開スナップショット（生 ``session_id`` を含まない）.

    「自分が保持者か」はこの値だけでは決まらない。クライアントは自身の
    ``key`` と ``key`` を比較して判定する。

    Attributes:
        key: 保持者の公開キー（未保持なら None）
        display_name: 保持者の表示名（未保持なら None）
        held: 誰かが保持しているか
        connections: 保持者の WebSocket 接続数（未保持なら 0）
    """

    key: str | None
    display_name: str | None
    held: bool
    connections: int


class ControlDeniedError(RuntimeError):
    """他クライアントが操作権を保持している（→ HTTP 423 Locked）."""

    def __init__(self, holder: str) -> None:
        """ControlDeniedError を初期化する.

        Args:
            holder: 操作権を保持しているクライアントの表示名
        """
        super().__init__(f"操作権は〈{holder}〉が保持しています")
        self._holder = holder

    @property
    def holder(self) -> str:
        """操作権を保持しているクライアントの表示名."""
        return self._holder


class ControlLease:
    """単一の操作権を貸し出すリース.

    保持者が画面を閉じたまま戻らないときは、他のクライアントが ``takeover`` で奪取する。
    WebSocket の接続数は ``LeaseInfo.connections`` に表示するためだけに数え、失効の判定には使わない。

    ``on_change`` の呼ばれ方:

    - ``claim`` / ``takeover`` / ``release`` の結果、保持者が変わったときだけ呼ぶ
    - 保持者の比較は ``ClientIdentity`` の値で行うので、同じ ``session_id`` でも ``display_name`` が変われば呼ぶ
    - 拒否された ``claim``、非保持者の ``release``、``connect`` / ``disconnect`` では呼ばない
    - 内部ロックを解放した後に呼ぶので、``on_change`` から ``snapshot`` を呼んでもデッドロックしない
    """

    def __init__(self, *, on_change: Callable[[], None] | None = None) -> None:
        """ControlLease を初期化する.

        Args:
            on_change: 保持者が変わったときに引数なしで呼ぶ通知（呼ばれ方はクラス docstring）
        """
        self._on_change = on_change
        self._lock = threading.Lock()
        self._holder: ClientIdentity | None = None
        self._connections: dict[str, int] = {}

    def snapshot(self) -> LeaseInfo:
        """現在のリース状態を返す."""
        with self._lock:
            return self._info()

    def claim(self, identity: ClientIdentity) -> LeaseInfo:
        """操作権を取得する（空きなら取得、保持者自身の呼び出しは表示名の更新）.

        Args:
            identity: 取得しようとするクライアント

        Returns:
            取得後のリース状態

        Raises:
            ControlDeniedError: 他クライアントが保持している場合
        """
        with self._lock:
            holder = self._holder
            changed = False
            if holder is not None and holder.session_id != identity.session_id:
                denied_by = holder.display_name
            else:
                denied_by = None
                changed = self._assign(identity)
            info = self._info()
        self._notify(changed)
        if denied_by is not None:
            raise ControlDeniedError(denied_by)
        return info

    def takeover(self, identity: ClientIdentity) -> LeaseInfo:
        """保持者を問わず操作権を奪取する（誰も操作できなくなった状態から抜け出す手段）.

        実行中のジョブには触らない。移るのは指示を出す権利だけで、走っている処理は止まらない。

        Args:
            identity: 奪取するクライアント

        Returns:
            奪取後のリース状態
        """
        with self._lock:
            changed = self._assign(identity)
            info = self._info()
        self._notify(changed)
        return info

    def release(self, identity: ClientIdentity) -> LeaseInfo:
        """自分が保持する操作権を解放する（冪等。非保持者の呼び出しは無効果）.

        Args:
            identity: 解放するクライアント

        Returns:
            解放後のリース状態
        """
        with self._lock:
            holder = self._holder
            changed = False
            if holder is not None and holder.session_id == identity.session_id:
                self._holder = None
                changed = True
            info = self._info()
        self._notify(changed)
        return info

    def connect(self, identity: ClientIdentity) -> None:
        """WebSocket 接続を数える（同一セッションの多重接続も数える）.

        Args:
            identity: 接続したクライアント
        """
        with self._lock:
            session_id = identity.session_id
            self._connections[session_id] = self._connections.get(session_id, 0) + 1

    def disconnect(self, identity: ClientIdentity) -> None:
        """WebSocket 切断を数える（保持者の接続数が 0 になっても操作権は保持したまま）.

        Args:
            identity: 切断したクライアント
        """
        with self._lock:
            session_id = identity.session_id
            remaining = self._connections.get(session_id, 0) - 1
            if remaining > 0:
                self._connections[session_id] = remaining
            else:
                self._connections.pop(session_id, None)

    def _assign(self, identity: ClientIdentity) -> bool:
        """保持者を ``identity`` にする（保持者が変わったら True）."""
        changed = self._holder != identity
        self._holder = identity
        return changed

    def _info(self) -> LeaseInfo:
        holder = self._holder
        if holder is None:
            return LeaseInfo(key=None, display_name=None, held=False, connections=0)
        return LeaseInfo(
            key=holder.key,
            display_name=holder.display_name,
            held=True,
            connections=self._connections.get(holder.session_id, 0),
        )

    def _notify(self, changed: bool) -> None:
        """保持者が変わったことを通知する（必ずロック解放後に呼ぶ）."""
        if changed and self._on_change is not None:
            self._on_change()
