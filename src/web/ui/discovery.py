"""LAN 上の backend WebAPI を mDNS (DNS-SD) で探索する.

ワイヤ定数（サービス型・TXT キー）は広告側（`web.api.discovery`）から import する
（定義箇所を 1 つに保つため、`web.ui` → `web.api` の import を許容する）。

**生存判定はしない。** PTR は other-TTL 4500s で残り、電源断では goodbye が飛ばない
ので、消えた機体は最大 75 分一覧に残る。到達不能なマシンを選ぶと 503 ページになる
（それが要件で、online/offline を推測して隠すより誤解が少ない）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence

from zeroconf import (
    Error as ZeroconfError,
    InterfaceChoice,
    IPVersion,
    ServiceInfo,
    ServiceStateChange,
    Zeroconf,
)
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from web.api.discovery import (
    SERVICE_TYPE,
    TXT_KEY_API,
    TXT_KEY_ID,
    TXT_KEY_NAME,
    TXT_KEY_TYPE,
)
from web.api.models import API_VERSION
from web.ui.machines import MachineEndpoint

logger = logging.getLogger(__name__)

# ServiceInfo の解決を待つ上限 [ms]（zeroconf の API は ms 指定）
_RESOLVE_TIMEOUT_MS = 3000.0


def _text(properties: Mapping[bytes, bytes | None], key: str) -> str | None:
    """TXT の 1 キーを文字列で読む（欠損・decode 不能・空は None）."""
    value = properties.get(key.encode())
    if value is None:
        return None
    try:
        return value.decode() or None
    except UnicodeDecodeError:
        return None


def _instance_id(info: ServiceInfo) -> str:
    """Instance 名（``info.name`` のサービス型より手前）."""
    return info.name.removesuffix(f".{info.type}")


def _api_compatible(properties: Mapping[bytes, bytes | None]) -> bool:
    """広告の ``api`` がこの frontend で描ける版か.

    欠損・decode 不能・数値でない場合は互換扱いで通す（キー欠損はフォールバック）。 読めて **違う版のときだけ**
    落とす（描けない backend を一覧に出さない）。
    """
    raw = _text(properties, TXT_KEY_API)
    if raw is None:
        return True
    try:
        return int(raw) == API_VERSION
    except ValueError:
        return True


def endpoint_from_service_info(info: ServiceInfo) -> MachineEndpoint | None:
    """解決済みの ServiceInfo を MachineEndpoint に変換する（純関数）.

    Args:
        info: 解決済みの ServiceInfo（TXT は bytes）

    Returns:
        変換した MachineEndpoint。machine_id が判らない / IPv4 アドレスが無い /
        port が無い / API 版が違う場合は None（一覧に出さない）
    """
    properties = info.properties
    if not _api_compatible(properties):
        return None
    machine_id = _text(properties, TXT_KEY_ID) or _instance_id(info)
    if not machine_id:
        return None
    # loopback を除外しない（E2E がループバックに閉じるため）
    addresses = info.parsed_addresses(IPVersion.V4Only)
    if not addresses or not info.port:
        return None
    return MachineEndpoint(
        machine_id=machine_id,
        host=addresses[0],
        port=info.port,
        name=_text(properties, TXT_KEY_NAME),
        machine_type=_text(properties, TXT_KEY_TYPE),
        source="mdns",
    )


class MachineDiscovery:
    """MDNS で見つけた backend を集めて変化のたびに通知する.

    `start` / `stop` は frontend の lifespan から await する。通知先は
    `MachineRegistry.set_discovered`（同期呼び出し）。
    """

    def __init__(
        self,
        *,
        on_change: Callable[[tuple[MachineEndpoint, ...]], None],
        service_type: str = SERVICE_TYPE,
        interfaces: Sequence[str] | None = None,
    ) -> None:
        """探索の設定を保持する（ソケットは `start` まで開かない）.

        Args:
            on_change: 発見集合が変わったときに呼ぶコールバック（発見順）
            service_type: DNS-SD のサービス型（テストはランダム型に閉じる）
            interfaces: 使うインターフェース（None は zeroconf 既定 = 全 IF。
                テストは ``["127.0.0.1"]`` でループバックに閉じる）
        """
        self._on_change = on_change
        self._service_type = service_type
        self._interfaces = tuple(interfaces) if interfaces is not None else None
        self._zeroconf: AsyncZeroconf | None = None
        self._browser: AsyncServiceBrowser | None = None
        # instance 名 → エンドポイント（挿入順 = 発見順を保つ）
        self._endpoints: dict[str, MachineEndpoint] = {}
        # 解決タスクの参照を保持する（保持しないと GC されて探索結果が欠ける）
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        """探索を開始する（失敗しても例外は投げない）.

        マルチキャストが使えない環境では WARNING を出して静的登録だけで続行する。
        """
        if self._zeroconf is not None:
            return
        zeroconf: AsyncZeroconf | None = None
        try:
            zeroconf = AsyncZeroconf(
                ip_version=IPVersion.V4Only,
                interfaces=(
                    list(self._interfaces)
                    if self._interfaces is not None
                    else InterfaceChoice.All
                ),
            )
            browser = AsyncServiceBrowser(
                zeroconf.zeroconf,
                self._service_type,
                handlers=[self._on_state_change],
            )
        except (OSError, ZeroconfError) as exc:
            logger.warning("mDNS 探索を開始できません: %s: %s", type(exc).__name__, exc)
            if zeroconf is not None:
                await zeroconf.async_close()
            return
        self._zeroconf = zeroconf
        self._browser = browser

    def _on_state_change(
        self,
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        """Zeroconf のブラウザコールバック（event loop 上で呼ばれる）."""
        if state_change is ServiceStateChange.Removed:
            if self._endpoints.pop(name, None) is not None:
                self._notify()
            return
        # Added / Updated は同扱い（TXT 更新も再解決して取り込む）
        task = asyncio.create_task(self._resolve(name))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _resolve(self, name: str) -> None:
        zeroconf = self._zeroconf
        if zeroconf is None:
            return
        info = AsyncServiceInfo(self._service_type, name)
        if not await info.async_request(zeroconf.zeroconf, _RESOLVE_TIMEOUT_MS):
            logger.warning("mDNS の解決に失敗しました: %s", name)
            return
        endpoint = endpoint_from_service_info(info)
        if endpoint is None:
            return
        if self._endpoints.get(name) == endpoint:
            return
        self._endpoints[name] = endpoint
        self._notify()

    def _notify(self) -> None:
        self._on_change(tuple(self._endpoints.values()))

    async def stop(self) -> None:
        """探索を止めてソケットを閉じる（未 start なら no-op）."""
        browser, zeroconf = self._browser, self._zeroconf
        self._browser = None
        self._zeroconf = None
        if browser is not None:
            await browser.async_cancel()
        if zeroconf is not None:
            await zeroconf.async_close()
