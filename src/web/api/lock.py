"""装置排他ロック（装置や設定を操作している処理を 1 つに限る）."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager


class BusyError(RuntimeError):
    """マシン排他ロックが取得できない（→ HTTP 409）."""

    def __init__(self, owner: str) -> None:
        super().__init__(f"装置は使用中です（owner: {owner}）")
        self._owner = owner

    @property
    def owner(self) -> str:
        return self._owner


class MachineLock:
    """非ブロッキングの装置排他ロック.

    ジョブは開始時（request スレッド）に :meth:`acquire` し、ワーカーの終了時（別スレッド）に :meth:`release` する。
    REST の操作は :meth:`hold` で同じロックを取る。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owner: str | None = None

    @property
    def owner(self) -> str | None:
        """ロックを保持している owner 名（未保持なら None）."""
        if self._lock.locked():
            return self._owner or "unknown"
        return None

    def acquire(self, owner: str) -> None:
        """ロックを非ブロッキングで取得する.

        Raises:
            BusyError: ロックが既に保持されている場合
        """
        if not self._lock.acquire(blocking=False):
            raise BusyError(self._owner or "unknown")
        self._owner = owner

    def release(self) -> None:
        """取得済みのロックを解放する（取得したスレッドとは別のスレッドからも解放できる）.

        Raises:
            RuntimeError: ロックが取得されていない場合
        """
        self._owner = None
        self._lock.release()

    @contextmanager
    def hold(self, owner: str) -> Iterator[None]:
        """ロックを取得し、抜けるときに解放する.

        Raises:
            BusyError: ロックが既に保持されている場合
        """
        self.acquire(owner)
        try:
            yield
        finally:
            self.release()
