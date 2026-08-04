"""操作権リース（複数ユーザー同時利用時の「操作は 1 セッションのみ」）.

閲覧は自由、変更系の操作は操作権を持つ 1 セッションだけに許す。緊急停止と abort
は安全のため誰でも常に可能なので、このモジュールでゲートしない。

FastAPI に依存しない純ロジック。時計とジョブ実行中判定を注入し、失効は 呼び出し時の lazy
判定だけで行う（バックグラウンドスレッドを持たない）。
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

    liveness は WebSocket の在線で判定する（クライアント側 heartbeat は持た
    ない）。保持者の接続数が 1 以上ある間は切断猶予による失効をしない。

    失効条件は次のいずれか（呼び出し時に判定する）。

    - 保持者の接続数が 0 になってから ``disconnect_grace`` 秒を超えた
    - 保持者の最終操作から ``idle_timeout`` 秒を超え、かつ ``busy()`` が False

    ``busy`` にジョブ実行中判定を注入することで、長時間ジョブの最中は無操作
    でも失効しない。
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        busy: Callable[[], bool],
        on_change: Callable[[], None] | None = None,
        disconnect_grace: float = 30.0,
        idle_timeout: float = 600.0,
    ) -> None:
        """ControlLease を初期化する.

        Args:
            clock: 単調増加する秒単位の時計（``time.monotonic`` を注入）
            busy: 装置がジョブ実行中かを返す判定
            on_change: 保持者が変わったときの通知（ロック解放後に呼ばれる）
            disconnect_grace: 保持者の接続数が 0 になってから失効までの秒数
            idle_timeout: 無操作で失効するまでの秒数（ジョブ実行中は失効しない）
        """
        self._clock = clock
        self._busy = busy
        self._on_change = on_change
        self._disconnect_grace = disconnect_grace
        self._idle_timeout = idle_timeout
        self._lock = threading.Lock()
        self._holder: ClientIdentity | None = None
        self._connections: dict[str, int] = {}
        self._last_active = 0.0
        # 保持者の接続数が 0 になった時刻（接続中は None）
        self._offline_since: float | None = None

    def snapshot(self) -> LeaseInfo:
        """現在のリース状態を返す（失効していれば解放してから返す）."""
        with self._lock:
            changed = self._expire_stale(self._clock())
            info = self._info()
        self._notify(changed)
        return info

    def claim(self, identity: ClientIdentity) -> LeaseInfo:
        """操作権を取得する（保持者自身の呼び出しは無操作時間の更新）.

        Args:
            identity: 取得しようとするクライアント

        Returns:
            取得後のリース状態

        Raises:
            ControlDeniedError: 他クライアントが保持している場合
        """
        with self._lock:
            now = self._clock()
            changed = self._expire_stale(now)
            holder = self._holder
            if holder is not None and holder.session_id != identity.session_id:
                denied_by = holder.display_name
            else:
                denied_by = None
                changed = self._assign(identity, now) or changed
            info = self._info()
        self._notify(changed)
        if denied_by is not None:
            raise ControlDeniedError(denied_by)
        return info

    def takeover(self, identity: ClientIdentity) -> LeaseInfo:
        """保持者を問わず操作権を奪取する（詰みからの脱出口）.

        実行中のジョブには一切触らない（指示を出す権利の移転であって、走って
        いる処理の移転ではない）。

        Args:
            identity: 奪取するクライアント

        Returns:
            奪取後のリース状態
        """
        with self._lock:
            now = self._clock()
            changed = self._expire_stale(now)
            changed = self._assign(identity, now) or changed
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
            changed = self._expire_stale(self._clock())
            holder = self._holder
            if holder is not None and holder.session_id == identity.session_id:
                self._clear()
                changed = True
            info = self._info()
        self._notify(changed)
        return info

    def connect(self, identity: ClientIdentity) -> None:
        """WebSocket 接続を登録する（同一セッションの多重接続を数える）.

        Args:
            identity: 接続したクライアント
        """
        with self._lock:
            changed = self._expire_stale(self._clock())
            session_id = identity.session_id
            self._connections[session_id] = self._connections.get(session_id, 0) + 1
            if self._holder is not None and self._holder.session_id == session_id:
                self._offline_since = None
        self._notify(changed)

    def disconnect(self, identity: ClientIdentity) -> None:
        """WebSocket 切断を登録する（保持者の接続数が 0 になれば猶予を開始）.

        Args:
            identity: 切断したクライアント
        """
        with self._lock:
            now = self._clock()
            changed = self._expire_stale(now)
            session_id = identity.session_id
            remaining = self._connections.get(session_id, 0) - 1
            if remaining > 0:
                self._connections[session_id] = remaining
            else:
                self._connections.pop(session_id, None)
                if self._holder is not None and self._holder.session_id == session_id:
                    self._offline_since = now
        self._notify(changed)

    def _expire_stale(self, now: float) -> bool:
        """失効していれば解放する（解放したら True）."""
        if self._holder is None:
            return False
        offline_since = self._offline_since
        if offline_since is not None and now - offline_since > self._disconnect_grace:
            self._clear()
            return True
        if now - self._last_active > self._idle_timeout and not self._busy():
            self._clear()
            return True
        return False

    def _assign(self, identity: ClientIdentity, now: float) -> bool:
        """保持者を ``identity`` にする（保持者が変わったら True）."""
        changed = self._holder != identity
        self._holder = identity
        self._last_active = now
        connected = self._connections.get(identity.session_id, 0) > 0
        self._offline_since = None if connected else now
        return changed

    def _clear(self) -> None:
        self._holder = None
        self._offline_since = None

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
