"""WS 購読者へのイベント配信."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

type Event = dict[str, Any]


class EventBus:
    """全 WS 購読者へイベントを配る唯一の配布点.

    ジョブのワーカー、machine.toml の書き込み、操作権リースなど、どのスレッドからでも :meth:`publish` できる。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """イベント配信先の event loop を登録する（lifespan startup で 1 回）."""
        with self._lock:
            self._loop = loop

    def subscribe(self) -> asyncio.Queue[Event]:
        """イベント購読キューを作成して返す."""
        events: asyncio.Queue[Event] = asyncio.Queue()
        with self._lock:
            self._subscribers.add(events)
        return events

    def unsubscribe(self, events: asyncio.Queue[Event]) -> None:
        """購読を解除する（未登録キューは無視）."""
        with self._lock:
            self._subscribers.discard(events)

    def publish(self, event: Event) -> None:
        """全 subscriber へイベントを配る（loop 未 bind なら何もしない）.

        ワーカースレッドからも呼ばれるので、キューへの投入は bind 済み loop の `call_soon_threadsafe` 経由。
        """
        with self._lock:
            loop = self._loop
            subscribers = tuple(self._subscribers)
        if loop is None:
            return
        for events in subscribers:
            try:
                loop.call_soon_threadsafe(events.put_nowait, event)
            except RuntimeError:
                # loop が閉じた後の遅延 publish は捨てる
                return

    def publish_state_changed(self) -> None:
        """マシン / PCB / 設定変更をクライアントに通知する."""
        self.publish({"type": "state_changed"})
